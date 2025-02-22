"""
pozoqod JSONRPC interface
"""
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'lib'))
import config
import base58
from bitcoinrpc.authproxy import AuthServiceProxy, JSONRPCException
from masternode import Masternode
from decimal import Decimal
import time
from deprecation import deprecated


class DashDaemon():
    def __init__(self, **kwargs):
        host = kwargs.get('host', '127.0.0.1')
        user = kwargs.get('user')
        password = kwargs.get('password')
        port = kwargs.get('port')

        self.creds = (user, password, host, port)

        # memoize calls to some pozoqod methods
        self.governance_info = None
        self.blockchain_info = None
        self.gobject_votes = {}

    @property
    def rpc_connection(self):
        return AuthServiceProxy("http://{0}:{1}@{2}:{3}".format(*self.creds))

    @classmethod
    def initialize(self, dash_dot_conf):
        # TODO: remove the return and raise an exception
        for var in ['RPCHOST', 'RPCUSER', 'RPCPASSWORD', 'RPCPORT']:
            if var not in os.environ:
                return self.from_dash_conf(dash_dot_conf)

        jsonrpc_creds = {}

        jsonrpc_creds['host'] = os.environ['RPCHOST']
        jsonrpc_creds['user'] = os.environ['RPCUSER']
        jsonrpc_creds['password'] = os.environ['RPCPASSWORD']
        jsonrpc_creds['port'] = os.environ['RPCPORT']

        return self(**jsonrpc_creds)

    @classmethod
    @deprecated(deprecated_in="1.7", details="Use environment variables to configure Sentinel instead.")
    def from_dash_conf(self, dash_dot_conf):
        from dash_config import DashConfig
        config_text = DashConfig.slurp_config_file(dash_dot_conf)
        creds = DashConfig.get_rpc_creds(config_text)

        creds[u'host'] = config.rpc_host

        return self(**creds)

    def rpc_command(self, *params):
        return self.rpc_connection.__getattr__(params[0])(*params[1:])

    # common RPC convenience methods

    def get_masternodes(self):
        mnlist = self.rpc_command('masternodelist', 'json')
        return [Masternode(k, v) for (k, v) in mnlist.items()]

    def get_current_masternode_outpoint(self):
        from dashlib import parse_masternode_status_outpoint

        my_outpoint = None

        try:
            status = self.rpc_command('masternode', 'status')
            my_outpoint = parse_masternode_status_outpoint(status.get('outpoint'))
        except JSONRPCException as e:
            pass

        return my_outpoint

    def governance_quorum(self):
        # TODO: expensive call, so memoize this
        total_masternodes = self.rpc_command('masternode', 'count')['enabled']
        min_quorum = self.govinfo['governanceminquorum']

        # the minimum quorum is calculated based on the number of masternodes
        quorum = max(min_quorum, (total_masternodes // 10))
        return quorum

    @property
    def govinfo(self):
        if (not self.governance_info):
            self.governance_info = self.rpc_command('getgovernanceinfo')
        return self.governance_info

    @property
    def blockchaininfo(self):
        if (not self.blockchain_info):
            self.blockchain_info = self.rpc_command('getblockchaininfo')
        return self.blockchain_info

    # governance info convenience methods
    def superblockcycle(self):
        return self.govinfo['superblockcycle']

    def network(self):
        # from dash/src/chainparamsbase.cpp
        # CBaseChainParams::MAIN = "main";
        # CBaseChainParams::TESTNET = "test";
        # CBaseChainParams::DEVNET = "devnet";
        # CBaseChainParams::REGTEST = "regtest";
        networks = {
            'test': 'testnet',
            'main': 'mainnet',
        }
        chain = self.blockchaininfo['chain']

        # returns 'testnet' and 'mainnet' instead of 'test' and 'main'
        return networks[chain] if chain in networks else chain

    def last_superblock_height(self):
        return self.govinfo['lastsuperblock']

    def next_superblock_height(self):
        return self.govinfo['nextsuperblock']

    def is_masternode(self):
        return not (self.get_current_masternode_outpoint() is None)

    def is_synced(self):
        return self.rpc_command('mnsync', 'status')['IsSynced']

    def current_block_hash(self):
        height = self.rpc_command('getblockcount')
        block_hash = self.rpc_command('getblockhash', height)
        return block_hash

    def get_superblock_budget_allocation(self, height=None):
        if height is None:
            height = self.rpc_command('getblockcount')
        return Decimal(self.rpc_command('getsuperblockbudget', height))

    def next_superblock_max_budget(self):
        return self.get_superblock_budget_allocation(self.next_superblock_height())

    # "my" votes refers to the current running masternode
    # memoized on a per-run, per-object_hash basis
    def get_my_gobject_votes(self, object_hash):
        import dashlib
        if not self.gobject_votes.get(object_hash):
            my_outpoint = self.get_current_masternode_outpoint()
            # if we can't get MN outpoint from output of `masternode status`,
            # return an empty list
            if not my_outpoint:
                return []

            (txid, vout_index) = my_outpoint.split('-')

            cmd = ['gobject', 'getcurrentvotes', object_hash, txid, vout_index]
            raw_votes = self.rpc_command(*cmd)
            self.gobject_votes[object_hash] = dashlib.parse_raw_votes(raw_votes)

        return self.gobject_votes[object_hash]

    def is_govobj_maturity_phase(self):
        # 3-day period for govobj maturity
        maturity_phase_delta = 1662      # ~(60*24*3)/2.6
        if self.network() == 'testnet':
            maturity_phase_delta = 24    # testnet

        event_block_height = self.next_superblock_height()
        maturity_phase_start_block = event_block_height - maturity_phase_delta

        current_height = self.rpc_command('getblockcount')
        event_block_height = self.next_superblock_height()

        # print "current_height = %d" % current_height
        # print "event_block_height = %d" % event_block_height
        # print "maturity_phase_delta = %d" % maturity_phase_delta
        # print "maturity_phase_start_block = %d" % maturity_phase_start_block

        return (current_height >= maturity_phase_start_block)

    def we_are_the_winner(self):
        import dashlib
        # find the elected MN outpoint for superblock creation...
        current_block_hash = self.current_block_hash()
        mn_list = self.get_masternodes()
        winner = dashlib.elect_mn(block_hash=current_block_hash, mnlist=mn_list)
        my_outpoint = self.get_current_masternode_outpoint()

        # print "current_block_hash: [%s]" % current_block_hash
        # print "MN election winner: [%s]" % winner
        # print "current masternode outpoint: [%s]" % my_outpoint

        return (winner == my_outpoint)

    def estimate_block_time(self, height):
        import dashlib
        """
        Called by block_height_to_epoch if block height is in the future.
        Call `block_height_to_epoch` instead of this method.

        DO NOT CALL DIRECTLY if you don't want a "Oh Noes." exception.
        """
        current_block_height = self.rpc_command('getblockcount')
        diff = height - current_block_height

        if (diff < 0):
            raise Exception("Oh Noes.")

        future_seconds = dashlib.blocks_to_seconds(diff)
        estimated_epoch = int(time.time() + future_seconds)

        return estimated_epoch

    def block_height_to_epoch(self, height):
        """
        Get the epoch for a given block height, or estimate it if the block hasn't
        been mined yet. Call this method instead of `estimate_block_time`.
        """
        epoch = -1

        try:
            bhash = self.rpc_command('getblockhash', height)
            block = self.rpc_command('getblock', bhash)
            epoch = block['time']
        except JSONRPCException as e:
            if e.message == 'Block height out of range':
                epoch = self.estimate_block_time(height)
            else:
                print("error: %s" % e)
                raise e

        return epoch
