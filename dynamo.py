import copy
import random
import logging
from history import History
import history
import logconfig
from node import Node
from timer import TimerManager
from framework import Framework
from hash_multiple import ConsistentHashTable
from dynamomessages import ClientPut, ClientGet, ClientPutRsp, ClientGetRsp
from dynamomessages import PutReq, GetReq, PutRsp, GetRsp
from dynamomessages import DynamoRequestMessage
from dynamomessages import PingReq, PingRsp
from vectorclock import VectorClock
from flask import Flask, request, jsonify, Response, render_template

logconfig.init_logging()
_logger = logging.getLogger('dynamo')

# PART dynamonode
class DynamoNode(Node):
    timer_priority = 20
    T = 10  # Number of repeats for nodes in consistent hash table
    N = 3  # Number of nodes to replicate at
    W = 2  # Number of nodes that need to reply to a write operation
    R = 2  # Number of nodes that need to reply to a read operation
    nodelist = []
    chash = ConsistentHashTable(nodelist, T)

    def __init__(self):
        super(DynamoNode, self).__init__()
        self.local_store = {}  # key => (value, metadata)
        self.pending_put_rsp = {}  # seqno => set of nodes that have stored
        self.pending_put_msg = {}  # seqno => original client message
        self.pending_get_rsp = {}  # seqno => set of (node, value, metadata) tuples
        self.pending_get_msg = {}  # seqno => original client message
        # seqno => set of requests sent to other nodes, for each message class
        self.pending_req = {PutReq: {}, GetReq: {}}
        self.failed_nodes = []
        self.pending_handoffs = {}
        # Rebuild the consistent hash table
        DynamoNode.nodelist.append(self)
        DynamoNode.chash = ConsistentHashTable(DynamoNode.nodelist, DynamoNode.T)
        # Run a timer to retry failed nodes
        self.retry_failed_node("retry")

# PART reset
    @classmethod
    def reset(cls):
        cls.nodelist = []
        cls.chash = ConsistentHashTable(cls.nodelist, cls.T)

# PART storage
    def store(self, key, value, metadata):
        self.local_store[key] = (value, metadata)

    def retrieve(self, key):
        if key in self.local_store:
            global found
            found = True
            return self.local_store[key]
        else:
            return (None, None)

# PART retry_failed_node
    def retry_failed_node(self, _):  # Permanently repeating timer
        if self.failed_nodes:
            node = self.failed_nodes.pop(0)
            # Send a test message to the oldest failed node
            pingmsg = PingReq(self, node)
            Framework.send_message(pingmsg)
        # Restart the timer
        TimerManager.start_timer(self, reason="retry", priority=15, callback=self.retry_failed_node)

    def rcv_pingreq(self, pingmsg):
        # Always reply to a test message
        pingrsp = PingRsp(pingmsg)
        Framework.send_message(pingrsp)

    def rcv_pingrsp(self, pingmsg):
        # Remove all instances of recovered node from failed node list
        recovered_node = pingmsg.from_node
        while recovered_node in self.failed_nodes:
            self.failed_nodes.remove(recovered_node)
        if recovered_node in self.pending_handoffs:
            for key in self.pending_handoffs[recovered_node]:
                # Send our latest value for this key
                (value, metadata) = self.retrieve(key)
                putmsg = PutReq(self, recovered_node, key, value, metadata)
                Framework.send_message(putmsg)
            del self.pending_handoffs[recovered_node]

# PART rsp_timer_pop
    def rsp_timer_pop(self, reqmsg):
        # no response to this request; treat the destination node as failed
        _logger.info("Node %s now treating node %s as failed", self, reqmsg.to_node)
        self.failed_nodes.append(reqmsg.to_node)
        failed_requests = Framework.cancel_timers_to(reqmsg.to_node)
        failed_requests.append(reqmsg)
        for failedmsg in failed_requests:
            self.retry_request(failedmsg)

    def retry_request(self, reqmsg):
        if not isinstance(reqmsg, DynamoRequestMessage):
            return
        # Send the request to an additional node by regenerating the preference list
        preference_list = DynamoNode.chash.find_nodes(reqmsg.key, DynamoNode.N, self.failed_nodes)[0]
        kls = reqmsg.__class__
        # Check the pending-request list for this type of request message
        if kls in self.pending_req and reqmsg.msg_id in self.pending_req[kls]:
            for node in preference_list:
                if node not in [req.to_node for req in self.pending_req[kls][reqmsg.msg_id]]:
                    # Found a node on the new preference list that hasn't been sent the request.
                    # Send it a copy
                    newreqmsg = copy.copy(reqmsg)
                    newreqmsg.to_node = node
                    self.pending_req[kls][reqmsg.msg_id].add(newreqmsg)
                    Framework.send_message(newreqmsg)

# PART rcv_clientput
    def rcv_clientput(self, msg):
        preference_list, avoided = DynamoNode.chash.find_nodes(msg.key, DynamoNode.N, self.failed_nodes)
        # Only track avoided nodes that would have been part of the original preference list
        avoided = avoided[:DynamoNode.N]
        non_extra_count = DynamoNode.N - len(avoided)
        # Determine if we are in the list
        if self not in preference_list:
            # Forward to the coordinator for this key
            _logger.info("put(%s=%s) maps to %s", msg.key, msg.value, preference_list)
            coordinator = preference_list[0]
            Framework.forward_message(msg, coordinator)
        else:
            # Use an incrementing local sequence number to distinguish
            # multiple requests for the same key
            seqno = self.generate_sequence_number()
            _logger.info("%s, %d: put %s=%s", self, seqno, msg.key, msg.value)
            # The metadata for a key is passed in by the client, and updated by the coordinator node.
            metadata = copy.deepcopy(msg.metadata)
            metadata.update(self.name, seqno)
            # Send out to preference list, and keep track of who has replied
            self.pending_req[PutReq][seqno] = set()
            self.pending_put_rsp[seqno] = set()
            self.pending_put_msg[seqno] = msg
            reqcount = 0
            for ii, node in enumerate(preference_list):
                if ii >= non_extra_count:
                    # This is an extra node that's only include because of a failed node
                    handoff = avoided
                else:
                    handoff = None
                # Send message to get node in preference list to store
                putmsg = PutReq(self, node, msg.key, msg.value, metadata, msg_id=seqno, handoff=handoff)
                self.pending_req[PutReq][seqno].add(putmsg)
                Framework.send_message(putmsg)
                reqcount = reqcount + 1
                if reqcount >= DynamoNode.N:
                    # preference_list may have more than N entries to allow for failed nodes
                    break

# PART rcv_clientget
    def rcv_clientget(self, msg):
        preference_list = DynamoNode.chash.find_nodes(msg.key, DynamoNode.N, self.failed_nodes)[0]
        # Determine if we are in the list
        if self not in preference_list:
            # Forward to the coordinator for this key
            _logger.info("get(%s=?) maps to %s", msg.key, preference_list)
            coordinator = preference_list[0]
            Framework.forward_message(msg, coordinator)
        else:
            seqno = self.generate_sequence_number()
            self.pending_req[GetReq][seqno] = set()
            self.pending_get_rsp[seqno] = set()
            self.pending_get_msg[seqno] = msg
            reqcount = 0
            for node in preference_list:
                getmsg = GetReq(self, node, msg.key, msg_id=seqno)
                self.pending_req[GetReq][seqno].add(getmsg)
                Framework.send_message(getmsg)
                reqcount = reqcount + 1
                if reqcount >= DynamoNode.N:
                    # preference_list may have more than N entries to allow for failed nodes
                    break

# PART rcv_put
    def rcv_put(self, putmsg):
        _logger.info("%s: store %s=%s", self, putmsg.key, putmsg.value)
        self.store(putmsg.key, putmsg.value, putmsg.metadata)
        if putmsg.handoff is not None:
            for failed_node in putmsg.handoff:
                self.failed_nodes.append(failed_node)
                if failed_node not in self.pending_handoffs:
                    self.pending_handoffs[failed_node] = set()
                self.pending_handoffs[failed_node].add(putmsg.key)
        putrsp = PutRsp(putmsg)
        global nodes
        nodes.append(self.name)
        Framework.send_message(putrsp)

# PART rcv_putrsp
    def rcv_putrsp(self, putrsp):
        seqno = putrsp.msg_id
        if seqno in self.pending_put_rsp:
            self.pending_put_rsp[seqno].add(putrsp.from_node)
            if len(self.pending_put_rsp[seqno]) >= DynamoNode.W:
                _logger.info("%s: written %d copies of %s=%s so done", self, DynamoNode.W, putrsp.key, putrsp.value)
                _logger.debug("  copies at %s", [node.name for node in self.pending_put_rsp[seqno]])
                # Tidy up tracking data structures
                original_msg = self.pending_put_msg[seqno]
                del self.pending_req[PutReq][seqno]
                del self.pending_put_rsp[seqno]
                del self.pending_put_msg[seqno]
                # Reply to the original client
                client_putrsp = ClientPutRsp(original_msg, putrsp.metadata)
                Framework.send_message(client_putrsp)
        else:
            pass  # Superfluous reply

# PART rcv_get
    def rcv_get(self, getmsg):
        _logger.info("%s: retrieve %s=?", self, getmsg.key)
        (value, metadata) = self.retrieve(getmsg.key)
        getrsp = GetRsp(getmsg, value, metadata)
        global nodes
        nodes.append(self.name)
        global retvalue
        retvalue = value
        global vclock
        vclock = metadata
        Framework.send_message(getrsp)

# PART rcv_getrsp
    def rcv_getrsp(self, getrsp):
        seqno = getrsp.msg_id
        if seqno in self.pending_get_rsp:
            self.pending_get_rsp[seqno].add((getrsp.from_node, getrsp.value, getrsp.metadata))
            if len(self.pending_get_rsp[seqno]) >= DynamoNode.R:
                _logger.info("%s: read %d copies of %s=? so done", self, DynamoNode.R, getrsp.key)
                _logger.debug("  copies at %s", [(node.name, value) for (node, value, _) in self.pending_get_rsp[seqno]])
                # Coalesce all compatible (value, metadata) pairs across the responses
                results = VectorClock.coalesce2([(value, metadata) for (node, value, metadata) in self.pending_get_rsp[seqno]])
                # Tidy up tracking data structures
                original_msg = self.pending_get_msg[seqno]
                del self.pending_req[GetReq][seqno]
                del self.pending_get_rsp[seqno]
                del self.pending_get_msg[seqno]
                # Reply to the original client, including all received values
                client_getrsp = ClientGetRsp(original_msg,
                                             [value for (value, metadata) in results],
                                             [metadata for (value, metadata) in results])
                Framework.send_message(client_getrsp)
        else:
            pass  # Superfluous reply

# PART rcvmsg
    def rcvmsg(self, msg):
        if isinstance(msg, ClientPut):
            self.rcv_clientput(msg)
        elif isinstance(msg, PutReq):
            self.rcv_put(msg)
        elif isinstance(msg, PutRsp):
            self.rcv_putrsp(msg)
        elif isinstance(msg, ClientGet):
            self.rcv_clientget(msg)
        elif isinstance(msg, GetReq):
            self.rcv_get(msg)
        elif isinstance(msg, GetRsp):
            self.rcv_getrsp(msg)
        elif isinstance(msg, PingReq):
            self.rcv_pingreq(msg)
        elif isinstance(msg, PingRsp):
            self.rcv_pingrsp(msg)
        else:
            raise TypeError("Unexpected message type %s", msg.__class__)

# PART get_contents
    def get_contents(self):
        results = []
        for key, value in self.local_store.items():
            results.append("%s:%s" % (key, value[0]))
        return results


# PART clientnode
class DynamoClientNode(Node):
    timer_priority = 17

    def __init__(self, name=None):
        super(DynamoClientNode, self).__init__(name)
        self.last_msg = None  # Track last received message

    def put(self, key, metadata, value, destnode=None):
        if destnode is None:  # Pick a random node to send the request to
            destnode = random.choice(DynamoNode.nodelist)
        # Input metadata is always a sequence, but we always need to insert a
        # single VectorClock object into the ClientPut message
        if len(metadata) == 1 and metadata[0] is None:
            metadata = VectorClock()
        else:
            # A Put operation always implies convergence
            metadata = VectorClock.converge(metadata)
        putmsg = ClientPut(self, destnode, key, value, metadata)
        Framework.send_message(putmsg)
        return putmsg

    def get(self, key, destnode=None):
        if destnode is None:
            destnode = random.choice(DynamoNode.nodelist)
        getmsg = ClientGet(self, destnode, key)
        Framework.send_message(getmsg)
        return getmsg

    def rsp_timer_pop(self, reqmsg):
        if isinstance(reqmsg, ClientPut):  # retry
            _logger.info("Put request timed out; retrying")
            self.put(reqmsg.key, [reqmsg.metadata], reqmsg.value)
        elif isinstance(reqmsg, ClientGet):  # retry
            _logger.info("Get request timed out; retrying")
            self.get(reqmsg.key)

# PART clientrcvmsg
    def rcvmsg(self, msg):
        self.last_msg = msg

# print('Start')

# DNode = DynamoNode()
# DNode2 = DynamoNode()
# DNode3 = DynamoNode()
# DNode4 = DynamoNode()
# client = DynamoClientNode()
# print('Putting')
# client.put("neel", (), "nfgnd", DNode2)
# Framework.schedule()
# client.put("neagsdgel", (), "dbab")
# Framework.schedule()
# client.put("sfgasgl", (), "ysn")
# Framework.schedule()
# client.put("sfgasgl", (), "nsy")
# Framework.schedule()
# client.put("bfdhgh", (), "gjks n")
# Framework.schedule()
# client.put("tyjl", (), "dgn")
# Framework.schedule()
# client.put("edfhadhahl", (), "dytsm")
# Framework.schedule()
# client.put("neel", (), "newnfgnd", DNode2)
# Framework.schedule()
# print('Getting')
# client.get("neel")
# Framework.schedule()
# client.get("badneel")
# Framework.schedule()

nodes = []
retvalue = ""
vclock = {}
found = False
app = Flask(__name__)

@app.route('/index')
def render_index():
    return render_template('index.html')

@app.route('/about')
def render_about():
    return render_template('about.html')

@app.route('/put', methods=['POST', 'GET'])
def put():
    if(request.method == 'POST'):
        key = request.form.get('key')
        value = request.form.get('value')
        global nodes
        nodes = []
        client = DynamoClientNode('client')
        client.put(key, (), value)
        Framework.schedule()
        return '''<H1>Dynamo</H1><br>
                    <H2>Response</H2><br>
                    <H2>Key: {}</H2>
                    <H2>Value: {}</H2>
                    <H2>Stored at: {}</H2><br>
                    <A href = "http://localhost:5000/put"> Put </A><br>
                    <A href = "http://localhost:5000/get"> Get </A>'''.format(key, value, nodes)

    return '''<H1>Dynamo</H1><br>
                <H2>PUT</H2><br>
                <form method="POST">
                    Key: <input type="text" name="key"><br>
                    Value: <input type="text" name="value"><br>
                    <input type="submit" value="Put"><br>
                </form>'''

@app.route('/get', methods=['POST', 'GET'])
def get():
    if(request.method == 'POST'):
        key = request.form.get('key')
        global nodes
        nodes = []
        global retvalue
        retvalue = ""
        global vclock
        vclock = {}
        global found
        found = False
        client = DynamoClientNode('client')
        client.get(key)
        Framework.schedule()
        if(found == False):
            return '''<H1>Dynamo</H1><br>
                        <H2>Response</H2><br>
                        <H2>Key: {}</H2>
                        <H2>Value: {}</H2>
                        <A href = "http://localhost:5000/put"> Put </A><br>
                        <A href = "http://localhost:5000/get"> Get </A>'''.format(key, "Not found")
        else:
            return '''<H1>Dynamo</H1><br>
                        <H2>Response</H2><br>
                        <H2>Key: {}</H2>
                        <H2>Value: {}</H2>
                        <H2>Retrieved from: {}</H2>
                        <H2>Coordinator Node and Vector Clock: {}</H2><br>
                        <A href = "http://localhost:5000/put"> Put </A><br>
                        <A href = "http://localhost:5000/get"> Get </A>'''.format(key, retvalue, nodes, vclock)

    return '''<H1>Dynamo</H1><br>
                <H2>GET</H2><br>
                <form method="POST">
                    Key: <input type="text" name="key"><br>
                    <input type="submit" value="Get"><br>
                </form>'''

if __name__ == '__main__':
    logconfig.init_logging()
    _logger = logging.getLogger('dynamo')
    DNode1 = DynamoNode()
    DNode2 = DynamoNode()
    DNode3 = DynamoNode()
    DNode4 = DynamoNode()
    DNode5 = DynamoNode()
    app.run(host="0.0.0.0", port=5000)
