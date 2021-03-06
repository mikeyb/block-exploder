import os
from gamecredits.helpers import has_length, is_block_file, calculate_target, calculate_work
import datetime
import itertools

MAIN_CHAIN = "main_chain"
PERSIST_EVERY = 1000  # blocks
RPC_USER = "62ca2d89-6d4a-44bd-8334-fa63ce26a1a3"
RPC_PASSWORD = "CsNa2vGB7b6BWUzN7ibfGuHbNBC1UJYZvXoebtTt1eup"
RPC_SYNC_PERCENT_DEFAULT = 97
MIN_STREAM_THRESH = 1500000


class Blockchain(object):
    def __init__(self, database):
        # Instance of MongoDatabaseGateway
        self.db = database

        # We keep a reference to the previous block so
        # we can update it's nextblockhash when we encounter the next block
        # without fetching it from the db - LOOK AHEAD THEN INSERT
        self.chain_peak = None

        # Flag to check if sync is in the first iteration
        self.first_iter = True

        # Global counter for creating unique identifiers
        self._counter = itertools.count()
        # Skip the taken identifiers
        while self._get_unique_chain_identifier() in self.db.get_chain_identifiers():
            pass

    def _create_coinbase(self, block):
        block.height = 0
        block.chainwork = block.work
        block.chain = MAIN_CHAIN
        self.chain_peak = block
        self.first_iter = False

        for tr in block.tx:
            self.db.put_transaction(tr)
        return block

    def _append_to_main_chain(self, block):
        self.chain_peak.nextblockhash = block.hash

        # Do not insert the chain_peak on first iter
        # because it's already fetched from the DB -> already exists
        if not self.first_iter:
            self.db.put_block(self.chain_peak)

        block.height = self.chain_peak.height + 1

        block.chainwork = self.chain_peak.chainwork + block.work
        block.chain = MAIN_CHAIN
        self.chain_peak = block
        return block

    def _get_unique_chain_identifier(self):
        return "chain%s" % next(self._counter)

    def insert_block(self, block):
        # Try to get the peak from the db
        if self.chain_peak is None:
            self.chain_peak = self.db.get_highest_block()

        # If it's still None then the db is empty
        # and we should create the coinbase block
        if self.chain_peak is None:
            added_block = self._create_coinbase(block)
            return {
                "block": added_block,
                "fork": "",
                "reconverge": False
            }

        # Current block appends to the main chain
        if block.previousblockhash == self.chain_peak.hash:
            added_block = self._append_to_main_chain(block)
            return {
                "block": added_block,
                "fork": "",
                "reconverge": False
            }
        # Current block is a fork
        else:
            fork_point = self.db.get_block_by_hash(block.previousblockhash)

            if fork_point.chain == MAIN_CHAIN:
                print "[FORK] Fork on block %s" % block.previousblockhash
                block.height = fork_point.height + 1
                block.chainwork = fork_point.chainwork + block.work
                block.chain = self._get_unique_chain_identifier()
            else:
                print "[FORK_GROW] A sidechain is growing."
                block.height = fork_point.height + 1
                block.chainwork = fork_point.chainwork + block.work
                block.chain = fork_point.chain
                self.db.update_block(block.previousblockhash, {"nextblockhash": block.hash})

            if block.chainwork > self.chain_peak.chainwork:
                print "[RECONVERGE] Reconverge, new top is now %s" % block.hash

                # Persist the previous block
                self.db.put_block(self.chain_peak)

                self.chain_peak = block
                block = self.reconverge(block)

                return {
                    "block": block,
                    "fork": fork_point.hash,
                    "reconverge": True
                }
            else:
                self.db.put_block(block)
                return {
                    "block": block,
                    "fork": fork_point.hash,
                    "reconverge": False
                }

        self.first_iter = False

    # OLD RECONVERGE IMPLEMENTATION
    # def reconverge(self, new_top_block):
    #     new_top_block.chain = MAIN_CHAIN
    #     first_in_sidechain_hash = new_top_block.hash
    #     current = self.db.get_block(new_top_block.previousblockhash)

    #     # Traverse up to the fork point and mark all nodes in sidechain as
    #     # part of main chain
    #     while current.chain != MAIN_CHAIN:
    #         self.db.update_block(current.hash, {"chain": MAIN_CHAIN})
    #         parent = self.db.get_block(current.previousblockhash)

    #         if parent.chain == MAIN_CHAIN:
    #             first_in_sidechain_hash = current.hash

    #         current = parent

    #     # Save the fork points hash
    #     fork_point_hash = current.hash

    #     # Traverse down the fork point and mark all main chain nodes part
    #     # of the sidechain
    #     while current.nextblockhash is not None:
    #         next_block = self.db.get_block(current.nextblockhash)
    #         self.db.update_block(next_block.hash, {"chain": self.num_forks + 1})
    #         current = next_block

    #     self.db.update_block(fork_point_hash, {"nextblockhash": first_in_sidechain_hash})

    #     self.num_convergences += 1

    #     return new_top_block

    def reconverge(self, new_top_block):
        sidechain_blocks = sorted(self.db.get_blocks_by_chain(chain=new_top_block.chain), key=lambda b: b.height)

        sidechain_blocks.append(new_top_block)

        first_in_sidechain = sidechain_blocks[0]
        fork_point = self.db.get_block_by_hash(first_in_sidechain.previousblockhash)
        main_chain_blocks = self.db.get_blocks_higher_than(height=fork_point.height)

        new_sidechain_id = self._get_unique_chain_identifier()
        for block in main_chain_blocks:
            self.db.update_block(block.hash, {"chain": new_sidechain_id})

        for block in sidechain_blocks:
            self.db.update_block(block.hash, {"chain": MAIN_CHAIN})
        self.db.update_block(fork_point.hash, {"nextblockhash": first_in_sidechain.hash})

        new_top_block.chain = MAIN_CHAIN
        return new_top_block


class ExploderSyncer(object):
    """
    Supports syncing from block dat files and using RPC.
    """
    def __init__(self, database, blockchain, blocks_dir, rpc_client, rpc_sync_percent=RPC_SYNC_PERCENT_DEFAULT):
        # Instance of MongoDatabaseGateway (to keep track of syncs)
        self.db = database

        # Reference to the Blockchain interactor
        self.blockchain = blockchain

        # Find all of the block dat files inside the given block directory
        if os.path.isdir(blocks_dir):
            self.blk_files = sorted(
                [os.path.join(blocks_dir, f) for f in os.listdir(blocks_dir) if is_block_file(f)]
            )
        else:
            raise Exception("[BLOCKCHAIN_INIT] Given path is not a directory")

        self.sync_progress = 0

        # When to stop reading from .dat files and start syncing from RPC
        self.rpc_sync_percent = rpc_sync_percent

        # Client RPC connection
        self.rpc = rpc_client

    ######################
    # SYNC METHODS       #
    ######################
    def sync_auto(self, limit=0):
        start_time = datetime.datetime.now()
        print "[SYNC_STARTED] %s" % start_time

        client_height = self.rpc.getblockcount()
        highest_known = self.db.highest_block

        if highest_known:
            self.sync_progress = float(highest_known.height * 100) / client_height
        else:
            self.sync_progress = 0

        parsed = 0
        if (highest_known and highest_known.height < MIN_STREAM_THRESH) or self.sync_progress < self.rpc_sync_percent:
            parsed = self._sync_stream(highest_known, client_height, limit)

        if (limit == 0 or parsed < limit):
            self._sync_rpc(client_height, limit)

        end_time = datetime.datetime.now()
        diff_time = end_time - start_time
        print "[SYNC_COMPLETE] %s, duration: %s seconds" % (end_time, diff_time.total_seconds())

    def sync_stream(self, highest_known, client_height, limit):
        print "[SYNC_STREAM] Started sync from .dat files"
        parsed = 0

        # Continue parsing where we left off
        if highest_known:
            self.blk_files = self.blk_files[highest_known.dat["index"]:]
            self._update_progress(highest_known.height, client_height)

        for (i, f) in enumerate(self.blk_files):
            stream = open(f, 'r')

            # Seek to the end of the last parsed block in the first iteration
            if i == 0 and highest_known:
                stream.seek(highest_known.dat['end'])

            while self._should_stream_sync(stream, limit, parsed):
                # parse block from stream
                res = BlockFactory.from_stream(stream)

                # Persist block and transactions
                block = self.handle_stream_block(res['block'])
                self.db.put_transactions(res['transactions'])

                parsed += 1

                # Update and print progress if necessary
                self._update_progress(block.height, client_height)

                if block.height % 1000 == 0:
                    self._print_progress()

        self.db.flush_cache()
        return parsed

    def sync_rpc(self, client_height, limit):
        print "[SYNC_RPC] Started sync from rpc"

        our_highest_block = self.db.highest_block
        our_highest_block_in_rpc = self._get_rpc_block_by_hash(our_highest_block.hash)

        # Compare our highest known block to it's repr in RPC
        # if they have the same height and previousblockhash there was no reconverge
        # and we can just insert the blocks sequentially by following the nextblockhash links
        if our_highest_block == our_highest_block_in_rpc:
            if our_highest_block_in_rpc.nextblockhash:
                next_block = self._get_rpc_block_by_hash(our_highest_block_in_rpc.nextblockhash)
                self.db.update_block(our_highest_block.hash, {"nextblockhash": next_block.hash})
                self._follow_chain_and_insert(start=next_block, limit=limit)
        else:
            print("[SYNC_RPC] Reconverge")
            # Else find the reconverge point by going backwards and finding the first block
            # that is the same in our db and rpc and then sync upwards from there
            (our_block, rpc_block) = self._find_reconverge_point(start=our_highest_block)

            # Delete all blocks upwards from reconverge point (including the point)
            self._follow_chain_and_delete(our_block)

            # Insert all blocks from rpc to the end of the chain
            self._follow_chain_and_insert(rpc_block, limit=limit)

        self.db.flush_cache()

    ######################
    #  HELPER FUNCTIONS  #
    ######################
    def _update_progress(self, current_height, client_height):
        self.sync_progress = float(current_height * 100) / client_height

    def _print_progress(self):
        print "Progress: %s%%" % self.sync_progress

    def _should_stream_sync(self, stream, limit, parsed):
        return self.sync_progress < self.rpc_sync_percent and \
            has_length(stream, 80) and (limit == 0 or parsed < limit)
