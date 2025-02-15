import json
import os
import time
from binascii import unhexlify
from collections import deque
from contextlib import closing
from functools import partial
from typing import Dict, Any, Mapping, Iterable, List, Optional, Set, Tuple, Callable

import gc
import psutil

from plenum.common.event_bus import InternalBus
from plenum.common.messages.internal_messages import NeedBackupCatchup, NeedMasterCatchup
from plenum.server.consensus.primary_selector import RoundRobinPrimariesSelector, PrimariesSelector
from plenum.server.database_manager import DatabaseManager
from plenum.server.node_bootstrap import NodeBootstrap
from plenum.server.replica import Replica

from common.exceptions import LogicError
from common.serializers.serialization import state_roots_serializer
from crypto.bls.bls_key_manager import LoadBLSKeyError
from plenum.common.gc_trackers import GcTimeTracker, GcObjectTree
from plenum.common.metrics_collector import KvStoreMetricsCollector, NullMetricsCollector, MetricsName, \
    async_measure_time, measure_time
from plenum.common.timer import QueueTimer
from plenum.server.backup_instance_faulty_processor import BackupInstanceFaultyProcessor
from plenum.server.batch_handlers.three_pc_batch import ThreePcBatch
from plenum.server.inconsistency_watchers import NetworkInconsistencyWatcher
from plenum.server.last_sent_pp_store_helper import LastSentPpStoreHelper
from plenum.server.quota_control import StaticQuotaControl, RequestQueueQuotaControl
from plenum.server.request_handlers.utils import VALUE
from plenum.server.request_managers.action_request_manager import ActionRequestManager
from plenum.server.request_managers.read_request_manager import ReadRequestManager
from plenum.server.request_managers.write_request_manager import WriteRequestManager
from plenum.server.view_change.node_view_changer import create_view_changer
from state.pruning_state import PruningState
from storage.helper import initKeyValueStorage, initHashStore, initKeyValueStorageIntKeys
from storage.state_ts_store import StateTsDbStorage
from stp_core.common.log import getlogger
from stp_core.crypto.signer import Signer
from stp_core.network.exceptions import RemoteNotFound
from stp_core.network.network_interface import NetworkInterface
from stp_core.types import HA
from stp_zmq.zstack import ZStack, Quota
from ledger.hash_stores.hash_store import HashStore

from plenum.common.config_util import getConfig
from plenum.common.constants import POOL_LEDGER_ID, DOMAIN_LEDGER_ID, \
    CLIENT_BLACKLISTER_SUFFIX, CONFIG_LEDGER_ID, \
    NODE_BLACKLISTER_SUFFIX, NODE_PRIMARY_STORAGE_SUFFIX, \
    TXN_TYPE, LEDGER_STATUS, \
    CLIENT_STACK_SUFFIX, PRIMARY_SELECTION_PREFIX, VIEW_CHANGE_PREFIX, \
    OP_FIELD_NAME, CATCH_UP_PREFIX, NYM, \
    GET_TXN, DATA, VERKEY, \
    TARGET_NYM, ROLE, STEWARD, TRUSTEE, ALIAS, \
    NODE_IP, BLS_PREFIX, NodeHooks, LedgerState, CURRENT_PROTOCOL_VERSION, AUDIT_LEDGER_ID, \
    AUDIT_TXN_VIEW_NO, AUDIT_TXN_PP_SEQ_NO, \
    TXN_AUTHOR_AGREEMENT_VERSION, AML, TXN_AUTHOR_AGREEMENT_TEXT, TS_LABEL, SEQ_NO_DB_LABEL, NODE_STATUS_DB_LABEL, \
    LAST_SENT_PP_STORE_LABEL, AUDIT_TXN_PRIMARIES
from plenum.common.exceptions import SuspiciousNode, SuspiciousClient, \
    MissingNodeOp, InvalidNodeOp, InvalidNodeMsg, InvalidClientMsgType, \
    InvalidClientRequest, BaseExc, \
    InvalidClientMessageException, KeysNotFoundException as REx, BlowUp, SuspiciousPrePrepare, \
    TaaAmlNotSetError, InvalidClientTaaAcceptanceError, UnauthorizedClientRequest
from plenum.common.has_file_storage import HasFileStorage
from plenum.common.hook_manager import HookManager
from plenum.common.keygen_utils import areKeysSetup
from plenum.common.ledger import Ledger
from plenum.common.message_processor import MessageProcessor
from plenum.common.messages.node_message_factory import node_message_factory
from plenum.common.messages.node_messages import Nomination, Batch, Reelection, \
    Primary, RequestAck, RequestNack, Reject, Ordered, \
    Propagate, PrePrepare, Prepare, Commit, Checkpoint, Reply, InstanceChange, LedgerStatus, \
    ConsistencyProof, CatchupReq, CatchupRep, ViewChangeDone, \
    MessageReq, MessageRep, ThreePhaseType, BatchCommitted, \
    ObservedData, FutureViewChangeDone, BackupInstanceFaulty
from plenum.common.motor import Motor
from plenum.common.plugin_helper import loadPlugins
from plenum.common.request import Request, SafeRequest
from plenum.common.roles import Roles
from plenum.common.signer_simple import SimpleSigner
from plenum.common.stacks import nodeStackClass, clientStackClass
from plenum.common.startable import Status, Mode
from plenum.common.txn_util import idr_from_req_data, get_req_id, \
    get_seq_no, get_type, get_payload_data, \
    get_txn_time, get_digest, TxnUtilConfig, get_payload_digest
from plenum.common.types import PLUGIN_TYPE_VERIFICATION, \
    OPERATION, f
from plenum.common.util import friendlyEx, getMaxFailures, pop_keys, \
    compare_3PC_keys, get_utc_epoch
from plenum.common.verifier import DidVerifier
from plenum.common.config_helper import PNodeConfigHelper

from plenum.persistence.req_id_to_txn import ReqIdrToTxn
from plenum.persistence.storage import Storage, initStorage
from plenum.bls.bls_crypto_factory import create_default_bls_crypto_factory
from plenum.recorder.recorder import add_start_time, add_stop_time

from plenum.client.wallet import Wallet

from plenum.server.blacklister import Blacklister
from plenum.server.blacklister import SimpleBlacklister
from plenum.server.client_authn import ClientAuthNr, SimpleAuthNr, CoreAuthNr
from plenum.server.has_action_queue import HasActionQueue
from plenum.server.instances import Instances
from plenum.server.message_req_processor import MessageReqProcessor
from plenum.server.monitor import Monitor
from plenum.server.notifier_plugin_manager import notifierPluginTriggerEvents, \
    PluginManager
from plenum.server.observer.observable import Observable
from plenum.server.observer.observer_node import NodeObserver
from plenum.server.observer.observer_sync_policy import ObserverSyncPolicyType
from plenum.server.plugin.has_plugin_loader_helper import PluginLoaderHelper
from plenum.server.pool_manager import TxnPoolManager
from plenum.server.propagator import Propagator
from plenum.server.quorums import Quorums
from plenum.server.replicas import Replicas
from plenum.server.req_authenticator import ReqAuthenticator
from plenum.server.router import Router
from plenum.server.suspicion_codes import Suspicions
from plenum.server.validator_info_tool import ValidatorNodeInfoTool
from plenum.server.view_change.view_changer import ViewChanger

pluginManager = PluginManager()
logger = getlogger()


class Node(HasActionQueue, Motor, Propagator, MessageProcessor, HasFileStorage,
           PluginLoaderHelper, MessageReqProcessor, HookManager):
    """
    A node in a plenum system.
    """

    suspicions = {s.code: s.reason for s in Suspicions.get_list()}
    keygenScript = "init_plenum_keys"
    client_request_class = SafeRequest
    _info_tool_class = ValidatorNodeInfoTool
    # The order of ledger id in the following list determines the order in
    # which those ledgers will be synced. Think carefully before changing the
    # order.
    ledger_ids = [AUDIT_LEDGER_ID, POOL_LEDGER_ID, CONFIG_LEDGER_ID, DOMAIN_LEDGER_ID]
    _wallet_class = Wallet

    def __init__(self,
                 name: str,
                 clientAuthNr: ClientAuthNr = None,
                 ha: HA = None,
                 cliname: str = None,
                 cliha: HA = None,
                 config_helper=None,
                 ledger_dir: str = None,
                 keys_dir: str = None,
                 genesis_dir: str = None,
                 plugins_dir: str = None,
                 node_info_dir: str = None,
                 view_changer: ViewChanger = None,
                 pluginPaths: Iterable[str] = None,
                 storage: Storage = None,
                 config=None,
                 seed=None,
                 bootstrap_cls=NodeBootstrap):
        """
        Create a new node.
        """
        self.ha = ha
        self.cliname = cliname
        self.cliha = cliha
        self.timer = QueueTimer()
        self.poolManager = None  # type: TxnPoolManager
        self.ledgerManager = None
        self.bls_bft = None
        self.write_req_validator = None

        self.config_and_dirs_init(name, config, config_helper, ledger_dir, keys_dir,
                                  genesis_dir, plugins_dir, node_info_dir, pluginPaths)
        self.requestExecuter = {}  # type: Dict[int, Callable]

        self.metrics = self._createMetricsCollector()
        if self.config.METRICS_COLLECTOR_TYPE is not None:
            self._gc_time_tracker = GcTimeTracker(self.metrics)

        self._info_tool = self._info_tool_class(self)

        # init database and request managers
        self.db_manager = DatabaseManager()
        self.init_req_managers()
        # init storages and request handlers
        self._bootstrap_node(bootstrap_cls, storage)

        # ToDo: refactor this on pluggable req handler integration phase
        self.register_executer(POOL_LEDGER_ID, self.execute_pool_txns)

        Motor.__init__(self)

        self.nodeReg = self.poolManager.nodeReg
        self.nodeIds = self.poolManager._ordered_node_ids
        self.cliNodeReg = self.poolManager.cliNodeReg

        self.register_executer(DOMAIN_LEDGER_ID, self.execute_domain_txns)

        # Number of read requests the node has processed
        self.total_read_request_number = 0

        self.clientAuthNr = clientAuthNr or self.defaultAuthNr()

        self.addGenesisNyms()

        self._mode = None  # type: Optional[Mode]

        self.network_stacks_init(seed)

        HasActionQueue.__init__(self)

        Propagator.__init__(self, metrics=self.metrics)

        MessageReqProcessor.__init__(self, metrics=self.metrics)

        self.view_changer = view_changer

        self.nodeInBox = deque()
        self.clientInBox = deque()

        # 3PC state consistency watchdog based on network events
        self.network_i3pc_watcher = NetworkInconsistencyWatcher(self.on_inconsistent_3pc_state_from_network)

        self.setPoolParams()

        self.network_i3pc_watcher.connect(self.name)

        self.clientBlacklister = SimpleBlacklister(
            self.name + CLIENT_BLACKLISTER_SUFFIX)  # type: Blacklister

        self.nodeBlacklister = SimpleBlacklister(
            self.name + NODE_BLACKLISTER_SUFFIX)  # type: Blacklister

        self.nodeInfo = {
            'data': {}
        }

        self._view_changer = None  # type: ViewChanger
        self.primaries_selector = RoundRobinPrimariesSelector()  # type: PrimariesSelector

        self.instances = Instances()

        self.monitor_init(pluginPaths)

        self.internal_bus = self._init_internal_bus()

        self.replicas = self.create_replicas()

        # Need to keep track of the time when lost connection with primary,
        # help in voting for/against a view change on the master and removing
        # replica on a backup instance
        self.primaries_disconnection_times = []

        # Any messages that are intended for protocol instances not created.
        # Helps in cases where a new protocol instance have been added by a
        # majority of nodes due to joining of a new node, but some slow nodes
        # are not aware of it. Key is instance id and value is a deque
        # TODO is it possible for messages with current view number?
        self.msgsForFutureReplicas = {}

        # Requests that are to be given to the view_changer by the node
        self.msgsToViewChanger = deque()

        # do it after all states and BLS stores are created
        self.adjustReplicas(0, self.requiredNumberOfInstances)

        self.perfCheckFreq = self.config.PerfCheckFreq
        self.nodeRequestSpikeMonitorData = {
            'value': 0,
            'cnt': 0,
            'accum': 0
        }

        self.propagates_phase_req_timeout = self.config.PROPAGATES_PHASE_REQ_TIMEOUT
        self.propagates_phase_req_timeouts = 0
        self.ordering_phase_req_timeout = self.config.ORDERING_PHASE_REQ_TIMEOUT
        self.ordering_phase_req_timeouts = 0

        if self.config.OUTDATED_REQS_CHECK_ENABLED:
            self.startRepeating(self.check_outdated_reqs, self.config.OUTDATED_REQS_CHECK_INTERVAL)

        self.startRepeating(self.checkPerformance, self.perfCheckFreq)

        self.startRepeating(self.checkNodeRequestSpike,
                            self.config
                            .notifierEventTriggeringConfig[
                                'nodeRequestSpike']['freq'])

        self.startRepeating(self.flush_metrics, self.config.METRICS_FLUSH_INTERVAL)

        if config.GC_STATS_REPORT_INTERVAL > 0:
            self.startRepeating(self.report_gc_stats, config.GC_STATS_REPORT_INTERVAL)

        self.white_list_init()

        # Map of request identifier, request id to client name. Used for
        # dispatching the processed requests to the correct client remote
        self.requestSender = {}  # Dict[str, str]
        self.backup_instance_faulty_processor = BackupInstanceFaultyProcessor(self)

        self.routers_init()

        # Quotas control
        node_quota = Quota(count=config.NODE_TO_NODE_STACK_QUOTA,
                           size=config.NODE_TO_NODE_STACK_SIZE)
        client_quota = Quota(count=config.CLIENT_TO_NODE_STACK_QUOTA,
                             size=config.CLIENT_TO_NODE_STACK_SIZE)

        if config.ENABLE_DYNAMIC_QUOTAS:
            self.quota_control = RequestQueueQuotaControl(max_request_queue_size=config.MAX_REQUEST_QUEUE_SIZE,
                                                          max_node_quota=node_quota,
                                                          max_client_quota=client_quota)
        else:
            self.quota_control = StaticQuotaControl(node_quota=node_quota, client_quota=client_quota)

        # Any messages that are intended for view numbers higher than the
        # current view.
        self.msgsForFutureViews = {}

        plugins_to_load = self.config.PluginsToLoad if hasattr(self.config, "PluginsToLoad") else None
        tp = loadPlugins(self.plugins_dir, plugins_to_load)
        logger.info("total plugins loaded in node: {}".format(tp))
        # TODO: this is already happening in `start`, why here then?
        self.logNodeInfo()
        self._wallet = None

        # Number of rounds of catchup done during a view change.
        self.catchup_rounds_without_txns = 0
        # The start time of the catch-up during view change
        self._catch_up_start_ts = 0

        self._last_performance_check_data = {}

        self.init_ledger_manager()

        HookManager.__init__(self, NodeHooks.get_all_vals())

        self._observable = Observable()
        self._observer = NodeObserver(self)

        # List of current replica's primaries, used for persisting in audit ledger
        # and restoration current primaries from audit ledger
        self.primaries = []

        # Flag which node set, when it have set new primaries and need to send batch
        self.primaries_batch_needed = False

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, value):
        self._mode = value
        for r in self.replicas.values():
            r.set_mode(value)

    def config_and_dirs_init(self, name, config, config_helper, ledger_dir, keys_dir,
                             genesis_dir, plugins_dir, node_info_dir, pluginPaths):
        self.created = time.time()
        self.name = name
        self.last_prod_started = None
        self.config = config or getConfig()

        self.config_helper = config_helper or PNodeConfigHelper(self.name, self.config)

        self.ledger_dir = ledger_dir or self.config_helper.ledger_dir
        self.keys_dir = keys_dir or self.config_helper.keys_dir
        self.genesis_dir = genesis_dir or self.config_helper.genesis_dir
        self.plugins_dir = plugins_dir or self.config_helper.plugins_dir
        self.node_info_dir = node_info_dir or self.config_helper.node_info_dir

        if self.config.STACK_COMPANION == 1:
            add_start_time(self.ledger_dir, self.utc_epoch())

        self._view_change_timeout = self.config.VIEW_CHANGE_TIMEOUT

        HasFileStorage.__init__(self, self.ledger_dir)
        self.ensureKeysAreSetup()

    def network_stacks_init(self, seed):
        kwargs = dict(stackParams=self.poolManager.nstack,
                      msgHandler=self.handleOneNodeMsg,
                      registry=self.nodeReg,
                      metrics=self.metrics)
        cls = self.nodeStackClass
        kwargs.update(seed=seed)
        # noinspection PyCallingNonCallable
        self.nodestack = cls(**kwargs)
        self.nodestack.onConnsChanged = self.onConnsChanged

        kwargs = dict(
            stackParams=self.poolManager.cstack,
            msgHandler=self.handleOneClientMsg,
            # TODO, Reject is used when dynamic validation fails, use Reqnack
            msgRejectHandler=self.reject_client_msg_handler,
            metrics=self.metrics,
            timer=self.timer)
        cls = self.clientStackClass
        kwargs.update(seed=seed)

        # noinspection PyCallingNonCallable
        self.clientstack = cls(**kwargs)

    def monitor_init(self, pluginPaths):
        # QUESTION: Why does the monitor need blacklister?
        self.monitor = Monitor(self.name,
                               Delta=self.config.DELTA,
                               Lambda=self.config.LAMBDA,
                               Omega=self.config.OMEGA,
                               instances=self.instances,
                               nodestack=self.nodestack,
                               blacklister=self.nodeBlacklister,
                               nodeInfo=self.nodeInfo,
                               notifierEventTriggeringConfig=self.config.notifierEventTriggeringConfig,
                               pluginPaths=pluginPaths,
                               notifierEventsEnabled=self.config.SpikeEventsEnabled)

    def white_list_init(self):
        # BE CAREFUL HERE
        # This controls which message types are excluded from signature
        # verification. Expressly prohibited from being in this is
        # ClientRequest and Propagation, which both require client
        # signature verification
        self.authnWhitelist = (
            Nomination,
            Primary,
            Reelection,
            Batch,
            ViewChangeDone,
            PrePrepare,
            Prepare,
            Checkpoint,
            Commit,
            InstanceChange,
            LedgerStatus,
            ConsistencyProof,
            CatchupReq,
            CatchupRep,
            MessageReq,
            MessageRep,
            ObservedData,
            BackupInstanceFaulty
        )

    def routers_init(self):
        # CurrentState
        self.nodeMsgRouter = Router(
            (Propagate, self.processPropagate),
            (InstanceChange, self.sendToViewChanger),
            (ViewChangeDone, self.sendToViewChanger),
            (MessageReq, self.process_message_req),
            (MessageRep, self.process_message_rep),
            (PrePrepare, self.sendToReplica),
            (Prepare, self.sendToReplica),
            (Commit, self.sendToReplica),
            (Checkpoint, self.sendToReplica),
            (LedgerStatus, self.ledgerManager.processLedgerStatus),
            (ConsistencyProof, self.ledgerManager.processConsistencyProof),
            (CatchupReq, self.ledgerManager.processCatchupReq),
            (CatchupRep, self.ledgerManager.processCatchupRep),
            (ObservedData, self.send_to_observer),
            (BackupInstanceFaulty, self.backup_instance_faulty_processor.process_backup_instance_faulty_msg)
        )

        self.clientMsgRouter = Router(
            (Request, self.processRequest),
            (LedgerStatus, self.ledgerManager.processLedgerStatus),
            (CatchupReq, self.ledgerManager.processCatchupReq),
        )

    # LEDGERS
    @property
    def poolLedger(self):
        return self.db_manager.get_ledger(POOL_LEDGER_ID)

    @property
    def domainLedger(self):
        return self.db_manager.get_ledger(DOMAIN_LEDGER_ID)

    @property
    def configLedger(self):
        return self.db_manager.get_ledger(CONFIG_LEDGER_ID)

    @property
    def auditLedger(self):
        return self.db_manager.get_ledger(AUDIT_LEDGER_ID)

    @property
    def states(self):
        return self.db_manager.states

    # This is storage for storing map: timestamp/state.headHash
    # Now it used in domainLedger
    @property
    def stateTsDbStorage(self):
        return self.db_manager.get_store(TS_LABEL)

    @property
    def seqNoDB(self):
        return self.db_manager.get_store(SEQ_NO_DB_LABEL)

    @property
    def nodeStatusDB(self):
        return self.db_manager.get_store(NODE_STATUS_DB_LABEL)

    @property
    def txn_type_to_ledger_id(self):
        all_types = {}
        all_types.update(self.action_manager.type_to_ledger_id)
        all_types.update(self.write_manager.type_to_ledger_id)
        all_types.update(self.read_manager.type_to_ledger_id)
        return all_types

    @property
    def last_sent_pp_store_helper(self):
        return self.db_manager.get_store(LAST_SENT_PP_STORE_LABEL)

    # EXECUTERS
    def default_executer(self, three_pc_batch: ThreePcBatch):
        return self.commitAndSendReplies(three_pc_batch)

    def execute_pool_txns(self, three_pc_batch) -> List:
        """
        Execute a transaction that involves consensus pool management, like
        adding a node, client or a steward.

        :param ppTime: PrePrepare request time
        :param reqs_keys: requests keys to be committed
        """
        committed_txns = self.default_executer(three_pc_batch)
        for txn in committed_txns:
            self.poolManager.onPoolMembershipChange(txn)
        return committed_txns

    def execute_domain_txns(self, three_pc_batch) -> List:
        committed_txns = self.default_executer(three_pc_batch)

        # Refactor: This is only needed for plenum as some old style tests
        # require authentication based on an in-memory map. This would be
        # removed later when we migrate old-style tests
        for txn in committed_txns:
            if get_type(txn) == NYM:
                self.addNewRole(txn)

        return committed_txns

    @property
    def viewNo(self):
        return None if self.view_changer is None else self.view_changer.view_no

    # TODO not sure that this should be allowed
    @viewNo.setter
    def viewNo(self, value):
        self.view_changer.view_no = value

    @property
    def view_change_in_progress(self):
        if self.view_changer is None:
            return False
        return self.view_changer.view_change_in_progress

    @property
    def pre_view_change_in_progress(self):
        if self.view_changer is None:
            return False
        return self.view_changer.pre_view_change_in_progress

    def _add_config_ledger(self):
        self.ledgerManager.addLedger(
            CONFIG_LEDGER_ID,
            self.configLedger,
            preCatchupStartClbk=self.preConfigLedgerCatchup,
            postCatchupCompleteClbk=self.postConfigLedgerCaughtUp,
            postTxnAddedToLedgerClbk=self.postTxnFromCatchupAddedToLedger)
        self.on_new_ledger_added(CONFIG_LEDGER_ID)

    def prePoolLedgerCatchup(self, **kwargs):
        self.mode = Mode.discovering

    def preConfigLedgerCatchup(self, **kwargs):
        self.mode = Mode.syncing

    def preDomainLedgerCatchup(self, **kwargs):
        self.mode = Mode.syncing

    def preAuditLedgerCatchup(self, **kwargs):
        self.mode = Mode.discovering

    def postConfigLedgerCaughtUp(self, **kwargs):
        pass

    @property
    def configLedgerStatus(self):
        return self.build_ledger_status(CONFIG_LEDGER_ID)

    def reject_client_msg_handler(self, reason, frm):
        self.transmitToClient(Reject("", "", reason), frm)

    @property
    def id(self):
        if isinstance(self.poolManager, TxnPoolManager):
            return self.poolManager.id
        return None

    @property
    def wallet(self):
        if not self._wallet:
            wallet = self._wallet_class(self.name)
            # TODO: Should use DidSigner to move away from cryptonyms
            signer = SimpleSigner(seed=unhexlify(self.nodestack.keyhex))
            wallet.addIdentifier(signer=signer)
            self._wallet = wallet
        return self._wallet

    @property
    def ledger_summary(self):
        return [li.ledger_summary for li in
                self.ledgerManager.ledgerRegistry.values()]

    @property
    def view_changer(self) -> ViewChanger:
        return self._view_changer

    @view_changer.setter
    def view_changer(self, value):
        self._view_changer = value

    # EXTERNAL EVENTS

    def on_view_change_start(self):
        """
        Notifies node about the fact that view changed to let it
        prepare for election
        """
        self.view_changer.start_view_change_ts = self.utc_epoch()

        for replica in self.replicas.values():
            replica.on_view_change_start()
        logger.info("{} resetting monitor stats at view change start".format(self))
        self.monitor.reset()
        self.processStashedMsgsForView(self.viewNo)

        self.backup_instance_faulty_processor.restore_replicas()
        self.drop_primaries()

        pop_keys(self.msgsForFutureViews, lambda x: x <= self.viewNo)
        self.logNodeInfo()
        # Keep on doing catchup until >(n-f) nodes LedgerStatus same on have a
        # prepared certificate the first PRE-PREPARE of the new view
        logger.info('{}{} changed to view {}, will start catchup now'.
                    format(VIEW_CHANGE_PREFIX, self, self.viewNo))

        self._cancel(self._check_view_change_completed)
        self.schedule_view_change_completion_check(self._view_change_timeout)

        # Set to 0 even when set to 0 in `on_view_change_complete` since
        # catchup might be started due to several reasons.
        self.catchup_rounds_without_txns = 0
        self.last_sent_pp_store_helper.erase_last_sent_pp_seq_no()

    def on_view_change_complete(self):
        """
        View change completes for a replica when it has been decided which was
        the last ppSeqNo and state and txn root for previous view
        """

        self.write_manager.future_primary_handler.set_node_state()

        if not self.replicas.all_instances_have_primary:
            raise LogicError(
                "{} Not all replicas have "
                "primaries: {}".format(self, self.replicas.primary_name_by_inst_id)
            )

        self._cancel(self._check_view_change_completed)

        for replica in self.replicas.values():
            replica.on_view_change_done()
        self.view_changer.last_completed_view_no = self.view_changer.view_no
        # Remove already ordered requests from requests list after view change
        # If view change happen when one half of nodes ordered on master
        # instance and backup but other only on master then we need to clear
        # requests list.  We do this to stop transactions ordering  on backup
        # replicas that have already been ordered on master.
        # Test for this case in plenum/test/view_change/
        # test_no_propagate_request_on_different_last_ordered_before_vc.py
        for replica in self.replicas.values():
            replica.clear_requests_and_fix_last_ordered()
        self.monitor.reset()

    def schedule_view_change_completion_check(self, timeout):
        self._schedule(action=self._check_view_change_completed,
                       seconds=timeout)

    def on_view_propagated(self):
        """
        View change completes for a replica when it has been decided which was
        the last ppSeqNo and state and txn root for previous view
        """
        self.write_manager.future_primary_handler.set_node_state()

        if not self.replicas.all_instances_have_primary:
            raise LogicError(
                "{} Not all replicas have "
                "primaries: {}".format(self, self.replicas.primary_name_by_inst_id)
            )
        self._cancel(self._check_view_change_completed)

        for replica in self.replicas.values():
            replica.on_view_change_done()
        self.view_changer.last_completed_view_no = self.view_changer.view_no
        self.monitor.reset()

    def drop_primaries(self):
        for replica in self.replicas.values():
            replica.primaryName = None

    def ensure_primaries_dropped(self):
        if any(replica.primaryName is not None for replica in self.replicas.values()):
            raise LogicError('Primaries must be dropped')

    def ensure_primaries_set(self):
        if any(replica.primaryName is None for replica in self.replicas.values()):
            raise LogicError('Primaries must be set')

    def on_inconsistent_3pc_state_from_network(self):
        if self.config.ENABLE_INCONSISTENCY_WATCHER_NETWORK:
            self.on_inconsistent_3pc_state()

    def on_inconsistent_3pc_state(self):
        logger.warning("There is high probability that current 3PC state is inconsistent,"
                       "immediate restart is recommended")

    def create_replicas(self) -> Replicas:
        return Replicas(self, self.monitor, self.config, self.metrics)

    def utc_epoch(self) -> int:
        """
        Returns the UTC epoch according to it's local clock
        """
        return get_utc_epoch()

    def __repr__(self):
        return self.name

    def _get_state_ts_db_storage(self):

        domainTsStorage = initKeyValueStorageIntKeys(
            self.config.stateTsStorage,
            self.dataLocation,
            self.config.stateTsDbName,
            db_config=self.config.db_state_ts_db_config)

        configTsStorage = initKeyValueStorageIntKeys(
            self.config.stateTsStorage,
            self.dataLocation,
            self.config.configStateTsDbName,
            db_config=self.config.db_state_ts_db_config)

        return StateTsDbStorage(self.name,
                                {
                                    DOMAIN_LEDGER_ID: domainTsStorage,
                                    CONFIG_LEDGER_ID: configTsStorage
                                })

    def loadSeqNoDB(self):
        return ReqIdrToTxn(
            initKeyValueStorage(
                self.config.reqIdToTxnStorage,
                self.dataLocation,
                self.config.seqNoDbName,
                db_config=self.config.db_seq_no_db_config)
        )

    def loadNodeStatusDB(self):
        return initKeyValueStorage(self.config.nodeStatusStorage,
                                   self.dataLocation,
                                   self.config.nodeStatusDbName,
                                   db_config=self.config.db_node_status_db_config)

    def _createMetricsCollector(self):
        if self.config.METRICS_COLLECTOR_TYPE is None:
            return NullMetricsCollector()

        if self.config.METRICS_COLLECTOR_TYPE == 'kv':
            return KvStoreMetricsCollector(
                initKeyValueStorage(
                    self.config.METRICS_KV_STORAGE,
                    self.dataLocation,
                    self.config.METRICS_KV_DB_NAME,
                    db_config=self.config.METRICS_KV_CONFIG
                )
            )

        logger.warning("Unknown metrics collector type: {}".format(self.config.METRICS_COLLECTOR_TYPE))
        return NullMetricsCollector()

    # noinspection PyAttributeOutsideInit
    def setPoolParams(self):
        # TODO should be always called when nodeReg is changed - automate
        self.allNodeNames = set(self.nodeReg.keys())
        self.network_i3pc_watcher.set_nodes(self.allNodeNames)
        self.totalNodes = len(self.allNodeNames)
        self.f = getMaxFailures(self.totalNodes)
        self.requiredNumberOfInstances = self.f + 1  # per RBFT
        self.minimumNodes = (2 * self.f) + 1  # minimum for a functional pool
        self.quorums = Quorums(self.totalNodes)
        logger.info(
            "{} updated its pool parameters: f {}, totalNodes {}, "
            "allNodeNames {}, requiredNumberOfInstances {}, minimumNodes {}, "
            "quorums {}".format(
                self, self.f, self.totalNodes,
                self.allNodeNames, self.requiredNumberOfInstances,
                self.minimumNodes, self.quorums))

    def build_ledger_status(self, ledger_id):
        ledger = self.getLedger(ledger_id)
        ledger_size = ledger.size
        v, p = (None, None)
        return LedgerStatus(ledger_id, ledger_size, v, p, ledger.root_hash, CURRENT_PROTOCOL_VERSION)

    @property
    def poolLedgerStatus(self):
        if self.poolLedger:
            return self.build_ledger_status(POOL_LEDGER_ID)

    @property
    def domainLedgerStatus(self):
        return self.build_ledger_status(DOMAIN_LEDGER_ID)

    def stateRootHash(self, ledgerId, isCommitted=True):
        state = self.states.get(ledgerId)
        if not state:
            raise RuntimeError('State with id {} does not exist')
        return state.committedHeadHash if isCommitted else state.headHash

    @property
    def is_synced(self):
        return Mode.is_done_syncing(self.mode)

    @property
    def isParticipating(self) -> bool:
        return self.mode == Mode.participating

    def start_participating(self):
        logger.info('{} started participating'.format(self))
        self.mode = Mode.participating

    @property
    def nodeStackClass(self) -> NetworkInterface:
        return nodeStackClass

    @property
    def clientStackClass(self) -> NetworkInterface:
        return clientStackClass

    def _add_pool_ledger(self):
        if isinstance(self.poolManager, TxnPoolManager):
            self.ledgerManager.addLedger(
                POOL_LEDGER_ID,
                self.poolLedger,
                preCatchupStartClbk=self.prePoolLedgerCatchup,
                postCatchupCompleteClbk=self.postPoolLedgerCaughtUp,
                postTxnAddedToLedgerClbk=self.postTxnFromCatchupAddedToLedger)
            self.on_new_ledger_added(POOL_LEDGER_ID)

    def _add_domain_ledger(self):
        self.ledgerManager.addLedger(
            DOMAIN_LEDGER_ID,
            self.domainLedger,
            preCatchupStartClbk=self.preDomainLedgerCatchup,
            postCatchupCompleteClbk=self.postDomainLedgerCaughtUp,
            postTxnAddedToLedgerClbk=self.postTxnFromCatchupAddedToLedger)
        self.on_new_ledger_added(DOMAIN_LEDGER_ID)

    def _add_audit_ledger(self):
        self.ledgerManager.addLedger(
            AUDIT_LEDGER_ID,
            self.auditLedger,
            preCatchupStartClbk=self.preAuditLedgerCatchup,
            postCatchupCompleteClbk=self.postAuditLedgerCaughtUp,
            postTxnAddedToLedgerClbk=self.postTxnFromCatchupAddedToLedger)
        self.on_new_ledger_added(AUDIT_LEDGER_ID)

    def getHashStore(self, name) -> HashStore:
        """
        Create and return a hashStore implementation based on configuration
        """
        return initHashStore(self.dataLocation, name, self.config)

    def init_ledger_manager(self):
        self._add_audit_ledger()
        self._add_pool_ledger()
        self._add_config_ledger()
        self._add_domain_ledger()

    def on_new_ledger_added(self, ledger_id):
        # If a ledger was added after a replicas were created
        self.replicas.register_new_ledger(ledger_id)

    def register_state(self, ledger_id, state):
        self.states[ledger_id] = state

    def register_executer(self, ledger_id: int, executer: Callable):
        self.requestExecuter[ledger_id] = executer

    def get_executer(self, ledger_id):
        executer = self.requestExecuter.get(ledger_id)
        if executer:
            return executer
        else:
            return self.default_executer

    def update_bls_key(self, new_bls_key):
        bls_keys_dir = os.path.join(self.keys_dir, self.name)
        bls_crypto_factory = create_default_bls_crypto_factory(bls_keys_dir)
        self.bls_bft.bls_crypto_signer = None

        try:
            bls_crypto_signer = bls_crypto_factory.create_bls_crypto_signer_from_saved_keys()
        except LoadBLSKeyError:
            logger.warning("{}Can not enable BLS signer on the Node. BLS keys are not initialized, "
                           "although NODE txn with blskey={} is sent. Please make sure that a script to init BLS keys (init_bls_keys) "
                           "was called ".format(BLS_PREFIX, new_bls_key))
            return

        if bls_crypto_signer.pk != new_bls_key:
            logger.warning("{}Can not enable BLS signer on the Node. BLS key initialized for the Node ({}), "
                           "differs from the one sent to the Ledger via NODE txn ({}). "
                           "Please make sure that a script to init BLS keys (init_bls_keys) is called, "
                           "and the same key is saved via NODE txn."
                           .format(BLS_PREFIX, bls_crypto_signer.pk, new_bls_key))
            return

        self.bls_bft.bls_crypto_signer = bls_crypto_signer
        logger.display("{}BLS key is rotated/set for Node {}. "
                       "BLS Signatures will be used for Node.".format(BLS_PREFIX, self.name))

    def ledger_id_for_request(self, request: Request):
        if request.operation.get(TXN_TYPE) is None:
            raise ValueError(
                "{} TXN_TYPE is not defined for request {}".format(self, request)
            )

        typ = request.operation[TXN_TYPE]
        return self.txn_type_to_ledger_id[typ]

    def start(self, loop):
        # Avoid calling stop and then start on the same node object as start
        # does not re-initialise states
        oldstatus = self.status
        if oldstatus in Status.going():
            logger.debug("{} is already {}, so start has no effect".
                         format(self, self.status.name))
        else:
            super().start(loop)

            # Start the ledgers
            for ledger in self.ledgers:
                ledger.start(loop)

            if self.nodeStatusDB and self.nodeStatusDB.closed:
                self.nodeStatusDB.open()

            self.nodestack.start()
            self.clientstack.start()

            self.view_changer = self.newViewChanger()
            self.schedule_initial_propose_view_change()

            self.schedule_node_status_dump()
            self.dump_additional_info()

            # if first time running this node
            if not self.nodestack.remotes:
                logger.info("{} first time running..." "".format(self), extra={
                    "cli": "LOW_STATUS", "tags": ["node-key-sharing"]})
            else:
                self.nodestack.maintainConnections(force=True)

            self.start_catchup(just_started=True)

        self.logNodeInfo()

    def schedule_initial_propose_view_change(self):
        # It is supposed that master's primary is lost until it is connected
        self.primaries_disconnection_times[self.master_replica.instId] = time.perf_counter()
        self._schedule(action=self.propose_view_change,
                       seconds=self.config.INITIAL_PROPOSE_VIEW_CHANGE_TIMEOUT)

    def schedule_node_status_dump(self):
        # one-shot dump right after start
        self._schedule(action=self._info_tool.dump_general_info,
                       seconds=self.config.DUMP_VALIDATOR_INFO_INIT_SEC)
        self.startRepeating(
            self._info_tool.dump_general_info,
            seconds=self.config.DUMP_VALIDATOR_INFO_PERIOD_SEC,
        )

    def dump_additional_info(self):
        self._info_tool.dump_additional_info()

    @property
    def rank(self) -> Optional[int]:
        return self.poolManager.rank

    def get_name_by_rank(self, rank, node_reg, node_ids):
        return self.poolManager.get_name_by_rank(rank, node_reg, node_ids)

    def get_rank_by_name(self, name, node_reg, node_ids):
        return self.poolManager.get_rank_by_name(name, node_reg, node_ids)

    def newViewChanger(self):
        if self.view_changer:
            return self.view_changer
        else:
            return create_view_changer(self)

    @property
    def connectedNodeCount(self) -> int:
        """
        The plus one is for this node, for example, if this node has three
        connections, then there would be four total nodes
        :return: number of connected nodes this one
        """
        return len(self.nodestack.conns) + 1

    @property
    def ledgers(self):
        return [self.ledgerManager.ledgerRegistry[lid].ledger
                for lid in self.ledger_ids if lid in self.ledgerManager.ledgerRegistry]

    def onStopping(self):
        """
        Actions to be performed on stopping the node.

        - Close the UDP socket of the nodestack
        """
        # Log stats should happen before any kind of reset or clearing
        if self.config.STACK_COMPANION == 1:
            add_stop_time(self.ledger_dir, self.utc_epoch())

        self.logstats()
        self.reset()

        # Stop the ledgers
        for ledger in self.ledgers:
            try:
                ledger.stop()
            except Exception as ex:
                logger.exception('{} got exception while stopping ledger: {}'.format(self, ex))

        self.nodestack.stop()
        self.clientstack.stop()

        self.closeAllKVStores()

        self._info_tool.stop()
        self.mode = None

    def closeAllKVStores(self):
        # Clear leveldb lock files
        logger.info("{} closing key-value storages".format(self), extra={"cli": False})
        self.db_manager.close()

    def reset(self):
        logger.info("{} reseting...".format(self), extra={"cli": False})
        self.nodestack.nextCheck = 0
        logger.debug(
            "{} clearing aqStash of size {}".format(
                self, len(
                    self.aqStash)))
        self.nodestack.conns.clear()
        # TODO: Should `self.clientstack.conns` be cleared too
        # self.clientstack.conns.clear()
        self.aqStash.clear()
        self.actionQueue.clear()
        self.view_changer = None

    @async_measure_time(MetricsName.NODE_PROD_TIME)
    async def prod(self, limit: int = None) -> int:
        """.opened
        This function is executed by the node each time it gets its share of
        CPU time from the event loop.

        :param limit: the number of items to be serviced in this attempt
        :return: total number of messages serviced by this node
        """
        c = 0

        if self.last_prod_started:
            self.metrics.add_event(MetricsName.LOOPER_RUN_TIME_SPENT, time.perf_counter() - self.last_prod_started)
        self.last_prod_started = time.perf_counter()

        self.quota_control.update_state({
            'request_queue_size': len(self.monitor.requestTracker.unordered())}
        )

        if self.status is not Status.stopped:
            c += await self.serviceReplicas(limit)
            c += await self.serviceNodeMsgs(limit)
            c += await self.serviceClientMsgs(limit)
            with self.metrics.measure_time(MetricsName.SERVICE_NODE_ACTIONS_TIME):
                c += self._serviceActions()
            with self.metrics.measure_time(MetricsName.SERVICE_TIMERS_TIME):
                self.timer.service()
            with self.metrics.measure_time(MetricsName.SERVICE_MONITOR_ACTIONS_TIME):
                c += self.monitor._serviceActions()
            c += await self.serviceViewChanger(limit)
            c += await self.service_observable(limit)
            c += await self.service_observer(limit)
            with self.metrics.measure_time(MetricsName.FLUSH_OUTBOXES_TIME):
                self.nodestack.flushOutBoxes()

        if self.isGoing():
            with self.metrics.measure_time(MetricsName.SERVICE_NODE_LIFECYCLE_TIME):
                self.nodestack.serviceLifecycle()
            with self.metrics.measure_time(MetricsName.SERVICE_CLIENT_STACK_TIME):
                self.clientstack.serviceClientStack()

        return c

    @async_measure_time(MetricsName.SERVICE_REPLICAS_TIME)
    async def serviceReplicas(self, limit) -> int:
        """
        Processes messages from replicas outbox and gives it time
        for processing inbox

        :param limit: the maximum number of messages to process
        :return: the sum of messages successfully processed
        """
        return self._process_replica_messages(limit)

    def _process_replica_messages(self, limit=None):
        inbox_processed = self.replicas.service_inboxes(limit)
        outbox_processed = self.service_replicas_outbox(limit)
        return outbox_processed + inbox_processed

    @async_measure_time(MetricsName.SERVICE_NODE_MSGS_TIME)
    async def serviceNodeMsgs(self, limit: int) -> int:
        """
        Process `limit` number of messages from the nodeInBox.

        :param limit: the maximum number of messages to process
        :return: the number of messages successfully processed
        """
        with self.metrics.measure_time(MetricsName.SERVICE_NODE_STACK_TIME):
            n = await self.nodestack.service(limit, self.quota_control.node_quota)

        self.metrics.add_event(MetricsName.NODE_STACK_MESSAGES_PROCESSED, n)

        await self.processNodeInBox()
        return n

    @async_measure_time(MetricsName.SERVICE_CLIENT_MSGS_TIME)
    async def serviceClientMsgs(self, limit: int) -> int:
        """
        Process `limit` number of messages from the clientInBox.

        :param limit: the maximum number of messages to process
        :return: the number of messages successfully processed
        """
        c = await self.clientstack.service(limit, self.quota_control.client_quota)
        self.metrics.add_event(MetricsName.CLIENT_STACK_MESSAGES_PROCESSED, c)

        await self.processClientInBox()
        return c

    @async_measure_time(MetricsName.SERVICE_VIEW_CHANGER_TIME)
    async def serviceViewChanger(self, limit) -> int:
        """
        Service the view_changer's inBox, outBox and action queues.

        :return: the number of messages successfully serviced
        """
        if not self.isReady():
            return 0
        o = self.serviceViewChangerOutBox(limit)
        i = await self.serviceViewChangerInbox(limit)
        return o + i

    @async_measure_time(MetricsName.SERVICE_OBSERVABLE_TIME)
    async def service_observable(self, limit) -> int:
        """
        Service the observable's inBox and outBox

        :return: the number of messages successfully serviced
        """
        if not self.isReady():
            return 0
        o = self._service_observable_out_box(limit)
        i = await self._observable.serviceQueues(limit)
        return o + i

    def _service_observable_out_box(self, limit: int = None) -> int:
        """
        Service at most `limit` number of messages from the observable's outBox.

        :return: the number of messages successfully serviced.
        """
        msg_count = 0
        while True:
            if limit and msg_count >= limit:
                break

            msg = self._observable.get_output()
            if not msg:
                break

            msg_count += 1
            msg, observer_ids = msg
            # TODO: it's assumed that all Observers are connected the same way as Validators
            self.sendToNodes(msg, observer_ids)
        return msg_count

    @async_measure_time(MetricsName.SERVICE_OBSERVER_TIME)
    async def service_observer(self, limit) -> int:
        """
        Service the observer's inBox and outBox

        :return: the number of messages successfully serviced
        """
        if not self.isReady():
            return 0
        return await self._observer.serviceQueues(limit)

    def onConnsChanged(self, joined: Set[str], left: Set[str]):
        """
        A series of operations to perform once a connection count has changed.

        - Set f to max number of failures this system can handle.
        - Set status to one of started, started_hungry or starting depending on
            the number of protocol instances.
        - Check protocol instances. See `checkInstances()`

        """
        _prev_status = self.status
        if self.isGoing():
            if self.connectedNodeCount == self.totalNodes:
                self.status = Status.started
            elif self.connectedNodeCount >= self.minimumNodes:
                self.status = Status.started_hungry
            else:
                self.status = Status.starting

        if self.master_primary_name in joined:
            self.primaries_disconnection_times[self.master_replica.instId] = None
        if self.master_primary_name in left:
            logger.display('{} lost connection to primary of master'.format(self))
            self.lost_master_primary()
        elif _prev_status == Status.starting and self.status == Status.started_hungry \
                and self.primaries_disconnection_times[self.master_replica.instId] is not None \
                and self.master_primary_name is not None:
            """
            Such situation may occur if the pool has come back to reachable consensus but
            primary is still disconnected, so view change proposal makes sense now.
            """
            self._schedule_view_change()

        for inst_id, replica in self.replicas.items():
            if not replica.isMaster and replica.primaryName is not None:
                primary_node_name = replica.primaryName.split(':')[0]
                if primary_node_name in joined:
                    self.primaries_disconnection_times[inst_id] = None
                elif primary_node_name in left:
                    self.primaries_disconnection_times[inst_id] = time.perf_counter()
                    self._schedule_replica_removal(inst_id)

        if self.isReady():
            self.checkInstances()
        else:
            logger.info("{} joined nodes {} but status is {}".format(self, joined, self.status))
        # Send ledger status whether ready (connected to enough nodes) or not
        for node in joined:
            self.send_ledger_status_to_newly_connected_node(node)

        for node in left:
            self.network_i3pc_watcher.disconnect(node)

        for node in joined:
            self.network_i3pc_watcher.connect(node)

    def request_ledger_status_from_nodes(self, ledger_id, nodes=None):
        for node_name in nodes if nodes else self.nodeReg:
            if node_name == self.name:
                continue
            try:
                self._ask_for_ledger_status(node_name, ledger_id)
            except RemoteNotFound:
                logger.debug(
                    '{} did not find any remote for {} to send '
                    'request for ledger status'.format(
                        self, node_name))
                continue

    def _ask_for_ledger_status(self, node_name: str, ledger_id):
        """
        Ask other node for LedgerStatus
        """
        self.request_msg(LEDGER_STATUS, {f.LEDGER_ID.nm: ledger_id},
                         [node_name, ])
        logger.info("{} asking {} for ledger status of ledger {}".format(self, node_name, ledger_id))

    def send_ledger_status_to_newly_connected_node(self, node_name):
        self.sendLedgerStatus(node_name, POOL_LEDGER_ID)

    def nodeJoined(self, txn_data):
        logger.display("{} new node joined by txn {}".format(self, txn_data))
        old_required_number_of_instances = self.requiredNumberOfInstances
        self.setPoolParams()
        self.adjustReplicas(old_required_number_of_instances,
                            self.requiredNumberOfInstances)
        self.select_primaries_if_needed(old_required_number_of_instances)

    def nodeLeft(self, txn_data):
        logger.display("{} node left by txn {}".format(self, txn_data))
        old_required_number_of_instances = self.requiredNumberOfInstances
        self.setPoolParams()
        self.adjustReplicas(old_required_number_of_instances,
                            self.requiredNumberOfInstances)
        self.select_primaries_if_needed(old_required_number_of_instances, txn_data)

    def select_primaries_if_needed(self, old_required_number_of_instances, txn_data=None):
        # This function mainly used in nodeJoined and nodeLeft functions
        leecher = self.ledgerManager._node_leecher._leechers[POOL_LEDGER_ID]
        alias = ""
        if txn_data and DATA in txn_data:
            alias = txn_data[DATA].get(ALIAS, alias)
        # If required number of instances changed, we need to recalculate it.
        if (self.requiredNumberOfInstances != old_required_number_of_instances or alias in self.primaries) \
                and not self.view_changer.view_change_in_progress \
                and leecher.state == LedgerState.synced:
            # We can call nodeJoined function during usual ordering or during catchup
            # We need to reselect primaries only during usual ordering. Because:
            # - If this is catchup, called by view change, then, we will select
            # primaries after it finish.
            # - If this is usual catchup, then, we will apply primaries from audit,
            # after catchup finish.
            self.view_changer.on_replicas_count_changed()

    @property
    def clientStackName(self):
        return self.getClientStackNameOfNode(self.name)

    @staticmethod
    def getClientStackNameOfNode(nodeName: str):
        return nodeName + CLIENT_STACK_SUFFIX

    def getClientStackHaOfNode(self, nodeName: str) -> HA:
        return self.cliNodeReg.get(self.getClientStackNameOfNode(nodeName))

    def _statusChanged(self, old: Status, new: Status) -> None:
        """
        Perform some actions based on whether this node is ready or not.

        :param old: the previous status
        :param new: the current status
        """

    def checkInstances(self) -> None:
        # TODO: Is this method really needed?
        """
        Check if this node has the minimum required number of protocol
        instances, i.e. f+1. If not, add a replica. If no election is in
        progress, this node will try to nominate one of its replicas as primary.
        This method is called whenever a connection with a  new node is
        established.
        """
        logger.debug("{} choosing to start election on the basis of count {} and nodes {}".
                     format(self, self.connectedNodeCount, self.nodestack.conns))

    def adjustReplicas(self,
                       old_required_number_of_instances: int,
                       new_required_number_of_instances: int):
        """
        Add or remove replicas depending on `f`
        """
        # TODO: refactor this
        replica_num = old_required_number_of_instances
        while replica_num < new_required_number_of_instances:
            self.replicas.add_replica(replica_num)
            self.processStashedMsgsForReplica(replica_num)
            replica_num += 1

        while replica_num > new_required_number_of_instances:
            replica_num -= 1
            self.replicas.remove_replica(replica_num)

        pop_keys(self.msgsForFutureReplicas, lambda inst_id: inst_id < new_required_number_of_instances)

        if len(self.primaries_disconnection_times) < new_required_number_of_instances:
            self.primaries_disconnection_times.extend(
                [None] * (new_required_number_of_instances - len(self.primaries_disconnection_times)))
        elif len(self.primaries_disconnection_times) > new_required_number_of_instances:
            self.primaries_disconnection_times = self.primaries_disconnection_times[:new_required_number_of_instances]

    def _dispatch_stashed_msg(self, msg, frm):
        # TODO DRY, in normal (non-stashed) case it's managed
        # implicitly by routes
        if isinstance(msg, (InstanceChange, ViewChangeDone)):
            self.sendToViewChanger(msg, frm)
            return True
        elif isinstance(msg, ThreePhaseType):
            self.sendToReplica(msg, frm)
            return True
        else:
            return False

    def processStashedMsgsForReplica(self, instId: int):
        if instId not in self.msgsForFutureReplicas:
            return
        i = 0
        while self.msgsForFutureReplicas[instId]:
            msg, frm = self.msgsForFutureReplicas[instId].popleft()
            if not self._dispatch_stashed_msg(msg, frm):
                self.discard(msg, reason="Unknown message type for replica id "
                                         "{}".format(instId),
                             logMethod=logger.warning)
            i += 1
        logger.info("{} processed {} stashed msgs for replica {}".format(self, i, instId))

    def processStashedMsgsForView(self, view_no: int):
        if view_no not in self.msgsForFutureViews:
            return
        i = 0
        while self.msgsForFutureViews[view_no]:
            msg, frm = self.msgsForFutureViews[view_no].popleft()
            if not self._dispatch_stashed_msg(msg, frm):
                self.discard(msg,
                             reason="{}Unknown message type for view no {}"
                             .format(VIEW_CHANGE_PREFIX, view_no),
                             logMethod=logger.warning)
            i += 1
        logger.info("{} processed {} stashed msgs for view no {}".format(self, i, view_no))

    def _check_view_change_completed(self):
        """
        This thing checks whether new primary was elected.
        If it was not - starts view change again
        """
        logger.info('{} running the scheduled check for view change completion'.format(self))
        if not self.view_changer.view_change_in_progress:
            logger.info('{} already completion view change'.format(self))
            return False

        self.view_changer.on_view_change_not_completed_in_time()
        return True

    @measure_time(MetricsName.SERVICE_REPLICAS_OUTBOX_TIME)
    def service_replicas_outbox(self, limit: int = None) -> int:
        """
        Process `limit` number of replica messages
        """
        # TODO: rewrite this using Router

        num_processed = 0
        for message in self.replicas.get_output(limit):
            num_processed += 1
            if isinstance(message, (PrePrepare, Prepare, Commit, Checkpoint)):
                self.send(message)
            elif isinstance(message, Ordered):
                self.try_processing_ordered(message)
            elif isinstance(message, tuple) and isinstance(message[1], Reject):
                with self.metrics.measure_time(MetricsName.NODE_SEND_REJECT_TIME):
                    digest, reject = message
                    result_reject = Reject(
                        reject.identifier,
                        reject.reqId,
                        self.reasonForClientFromException(
                            reject.reason))
                    # TODO: What the case when reqKey will be not in requestSender dict
                    if digest in self.requestSender:
                        self.transmitToClient(result_reject, self.requestSender[digest])
                        self.doneProcessingReq(digest)
            elif isinstance(message, Exception):
                self.processEscalatedException(message)
            else:
                # TODO: should not this raise exception?
                logger.error("Received msg {} and don't "
                             "know how to handle it".format(message))
        return num_processed

    def serviceViewChangerOutBox(self, limit: int = None) -> int:
        """
        Service at most `limit` number of messages from the view_changer's outBox.

        :return: the number of messages successfully serviced.
        """
        msgCount = 0
        while self.view_changer.outBox and (not limit or msgCount < limit):
            msgCount += 1
            msg = self.view_changer.outBox.popleft()
            if isinstance(msg, (InstanceChange, ViewChangeDone)):
                self.send(msg)
            else:
                logger.error("Received msg {} and don't know how to handle it".
                             format(msg))
        return msgCount

    async def serviceViewChangerInbox(self, limit: int = None) -> int:
        """
        Service at most `limit` number of messages from the view_changer's outBox.

        :return: the number of messages successfully serviced.
        """
        msgCount = 0
        while self.msgsToViewChanger and (not limit or msgCount < limit):
            msgCount += 1
            msg = self.msgsToViewChanger.popleft()
            self.view_changer.inBox.append(msg)
        await self.view_changer.serviceQueues(limit)
        return msgCount

    @property
    def hasPrimary(self) -> bool:
        """
        Whether this node has primary of any protocol instance
        """
        # TODO: remove this property?
        return self.replicas.some_replica_is_primary

    @property
    def has_master_primary(self) -> bool:
        """
        Whether this node has primary of master protocol instance
        """
        # TODO: remove this property?
        return self.replicas.master_replica_is_primary

    @property
    def master_primary_name(self) -> Optional[str]:
        """
        Return the name of the primary node of the master instance
        """

        master_primary_name = self.master_replica.primaryName
        if master_primary_name:
            return self.master_replica.getNodeName(master_primary_name)
        return None

    @property
    def master_last_ordered_3PC(self) -> Tuple[int, int]:
        return self.master_replica.last_ordered_3pc

    @property
    def master_replica(self):
        # TODO: this must be refactored.
        # Accessing Replica directly should be prohibited
        return self.replicas._master_replica

    def msgHasAcceptableInstId(self, msg, frm) -> bool:
        """
        Return true if the instance id of message corresponds to a correct
        replica.

        :param msg: the node message to validate
        :return:
        """
        # TODO: refactor this! this should not do anything except checking!
        instId = getattr(msg, f.INST_ID.nm, None)
        if not (isinstance(instId, int) and instId >= 0):
            return False
        if instId >= self.requiredNumberOfInstances:
            if instId not in self.msgsForFutureReplicas:
                self.msgsForFutureReplicas[instId] = deque()
            self.msgsForFutureReplicas[instId].append((msg, frm))
            logger.debug("{} queueing message {} for future protocol instance {}".format(self, msg, instId))
            return False
        return True

    def _is_initial_view_change_now(self):
        return (self.viewNo == 0) and (self.master_primary_name is None)

    def msgHasAcceptableViewNo(self, msg, frm) -> bool:
        """
        Return true if the view no of message corresponds to the current view
        no or a view no in the future
        :param msg: the node message to validate
        :return:
        """
        # TODO: refactor this! this should not do anything except checking!
        view_no = getattr(msg, f.VIEW_NO.nm, None)
        if not (isinstance(view_no, int) and view_no >= 0):
            return False
        if self.viewNo - view_no > 1:
            self.discard(msg, "un-acceptable viewNo {}"
                         .format(view_no), logMethod=logger.warning)
        if isinstance(msg, ViewChangeDone) and view_no < self.viewNo:
            self.discard(msg, "Proposed viewNo {} less, then current {}"
                         .format(view_no, self.viewNo), logMethod=logger.warning)
        elif (view_no > self.viewNo) or self._is_initial_view_change_now():
            if view_no not in self.msgsForFutureViews:
                self.msgsForFutureViews[view_no] = deque()
            logger.debug('{} stashing a message for a future view: {}'.format(self, msg))
            self.msgsForFutureViews[view_no].append((msg, frm))
            if isinstance(msg, ViewChangeDone):
                future_vcd_msg = FutureViewChangeDone(vcd_msg=msg)
                self.msgsToViewChanger.append((future_vcd_msg, frm))
        else:
            return True
        return False

    @measure_time(MetricsName.SEND_TO_REPLICA_TIME)
    def sendToReplica(self, msg, frm):
        """
        Send the message to the intended replica.

        :param msg: the message to send
        :param frm: the name of the node which sent this `msg`
        """
        # TODO: discard or stash messages here instead of doing
        # this in msgHas* methods!!!
        if self.msgHasAcceptableInstId(msg, frm):
            self.replicas.pass_message((msg, frm), msg.instId)

    def sendToViewChanger(self, msg, frm):
        """
        Send the message to the intended view changer.

        :param msg: the message to send
        :param frm: the name of the node which sent this `msg`
        """
        if (isinstance(msg, InstanceChange) or
                self.msgHasAcceptableViewNo(msg, frm)):
            logger.debug("{} sending message to view changer: {}".
                         format(self, (msg, frm)))
            self.msgsToViewChanger.append((msg, frm))

    def send_to_observer(self, msg, frm):
        """
        Send the message to the observer.

        :param msg: the message to send
        :param frm: the name of the node which sent this `msg`
        """
        logger.debug("{} sending message to observer: {}".
                     format(self, (msg, frm)))
        self._observer.append_input(msg, frm)

    def handleOneNodeMsg(self, wrappedMsg):
        """
        Validate and process one message from a node.

        :param wrappedMsg: Tuple of message and the name of the node that sent
        the message
        """
        try:
            vmsg = self.validateNodeMsg(wrappedMsg)
            if vmsg:
                logger.trace("{} msg validated {}".format(self, wrappedMsg),
                             extra={"tags": ["node-msg-validation"]})
                self.unpackNodeMsg(*vmsg)
            else:
                logger.debug("{} invalidated msg {}".format(self, wrappedMsg),
                             extra={"tags": ["node-msg-validation"]})
        except SuspiciousNode as ex:
            self.reportSuspiciousNodeEx(ex)
        except Exception as ex:
            msg, frm = wrappedMsg
            self.discard(msg, ex, logger.info)

    @measure_time(MetricsName.VALIDATE_NODE_MSG_TIME)
    def validateNodeMsg(self, wrappedMsg):
        """
        Validate another node's message sent to this node.

        :param wrappedMsg: Tuple of message and the name of the node that sent
        the message
        :return: Tuple of message from node and name of the node
        """
        msg, frm = wrappedMsg
        if self.isNodeBlacklisted(frm):
            self.discard(str(msg)[:256], "received from blacklisted node {}".format(frm), logger.display)
            return None

        with self.metrics.measure_time(MetricsName.INT_VALIDATE_NODE_MSG_TIME):
            try:
                message = node_message_factory.get_instance(**msg)
            except (MissingNodeOp, InvalidNodeOp) as ex:
                raise ex
            except Exception as ex:
                raise InvalidNodeMsg(str(ex))

        try:
            self.verifySignature(message)
        except BaseExc as ex:
            raise SuspiciousNode(frm, ex, message) from ex
        logger.debug("{} received node message from {}: {}".format(self, frm, message), extra={"cli": False})
        return message, frm

    def unpackNodeMsg(self, msg, frm) -> None:
        """
        If the message is a batch message validate each message in the batch,
        otherwise add the message to the node's inbox.

        :param msg: a node message
        :param frm: the name of the node that sent this `msg`
        """
        # TODO: why do we unpack batches here? Batching is a feature of
        # a transport, it should be encapsulated.

        if isinstance(msg, Batch):
            logger.trace("{} processing a batch {}".format(self, msg))
            with self.metrics.measure_time(MetricsName.UNPACK_BATCH_TIME):
                for m in msg.messages:
                    try:
                        m = self.nodestack.deserializeMsg(m)
                    except Exception as ex:
                        logger.warning("Got error {} while processing {} message".format(ex, m))
                        continue
                    self.handleOneNodeMsg((m, frm))
        else:
            self.postToNodeInBox(msg, frm)

    def postToNodeInBox(self, msg, frm):
        """
        Append the message to the node inbox

        :param msg: a node message
        :param frm: the name of the node that sent this `msg`
        """
        logger.trace("{} appending to nodeInbox {}".format(self, msg))
        self.nodeInBox.append((msg, frm))

    @async_measure_time(MetricsName.PROCESS_NODE_INBOX_TIME)
    async def processNodeInBox(self):
        """
        Process the messages in the node inbox asynchronously.
        """
        while self.nodeInBox:
            m = self.nodeInBox.popleft()
            await self.process_one_node_message(m)

    async def process_one_node_message(self, m):
        try:
            await self.nodeMsgRouter.handle(m)
        except SuspiciousNode as ex:
            self.reportSuspiciousNodeEx(ex)
            self.discard(m, ex, logger.debug)

    def handleOneClientMsg(self, wrappedMsg):
        """
        Validate and process a client message

        :param wrappedMsg: a message from a client
        """
        try:
            vmsg = self.validateClientMsg(wrappedMsg)
            if vmsg:
                self.unpackClientMsg(*vmsg)
        except BlowUp:
            raise
        except Exception as ex:
            msg, frm = wrappedMsg
            friendly = friendlyEx(ex)
            if isinstance(ex, SuspiciousClient):
                self.reportSuspiciousClient(frm, friendly)

            self.handleInvalidClientMsg(ex, wrappedMsg)

    def handleInvalidClientMsg(self, ex, wrappedMsg):
        msg, frm = wrappedMsg
        exc = ex.__cause__ if ex.__cause__ else ex
        friendly = friendlyEx(ex)
        reason = self.reasonForClientFromException(ex)
        if isinstance(msg, Request):
            msg = msg.as_dict
        identifier = idr_from_req_data(msg)
        # we send reqId == 1 when we need to reply on invalid LEDGER_STATUS
        reqId = msg.get(f.REQ_ID.nm) or 1
        if not reqId:
            reqId = getattr(exc, f.REQ_ID.nm, None)
            if not reqId:
                reqId = getattr(ex, f.REQ_ID.nm, None)
        self.send_nack_to_client((identifier, reqId), reason, frm)
        self.discard(wrappedMsg, friendly, logger.info, cliOutput=True)
        self._specific_invalid_client_msg_handling(ex, msg, frm)

    def _specific_invalid_client_msg_handling(self, ex, msg, frm):
        op = msg.get('op')
        if (op == LEDGER_STATUS):
            self._invalid_client_ledger_status_handling(ex, msg, frm)

    def _invalid_client_ledger_status_handling(self, ex, msg, frm):
        # This specific validation handles incorrect client LEDGER_STATUS message
        logger.info("{} received bad LEDGER_STATUS message from client {}. "
                    "Reason: {}. ".format(self, frm, ex.args[0]))
        # Since client can't yet handle denial of LEDGER_STATUS,
        # node send his LEDGER_STATUS back
        self.send_ledger_status_to_client(msg.get(f.LEDGER_ID.nm),
                                          msg.get(f.TXN_SEQ_NO.nm),
                                          msg.get(f.VIEW_NO.nm),
                                          msg.get(f.PP_SEQ_NO.nm),
                                          msg.get(f.MERKLE_ROOT.nm),
                                          CURRENT_PROTOCOL_VERSION,
                                          frm)

    def validateClientMsg(self, wrappedMsg):
        """
        Validate a message sent by a client.
        :param wrappedMsg: a message from a client
        :return: Tuple of clientMessage and client address
        """
        msg, frm = wrappedMsg
        if self.isClientBlacklisted(frm):
            self.discard(str(msg)[:256], "received from blacklisted client {}".format(frm), logger.display)
            return None

        needStaticValidation = False
        if all([msg.get(OPERATION), msg.get(f.REQ_ID.nm),
                idr_from_req_data(msg)]):
            cls = self.client_request_class
            needStaticValidation = True
        elif OP_FIELD_NAME in msg:
            op = msg[OP_FIELD_NAME]
            cls = node_message_factory.get_type(op)
            if cls not in (Batch, LedgerStatus, CatchupReq):
                raise InvalidClientMsgType(cls, msg.get(f.REQ_ID.nm))
        else:
            raise InvalidClientRequest(msg.get(f.IDENTIFIER.nm),
                                       msg.get(f.REQ_ID.nm))
        try:
            cMsg = cls(**msg)
        except TypeError as ex:
            raise InvalidClientRequest(msg.get(f.IDENTIFIER.nm),
                                       msg.get(f.REQ_ID.nm),
                                       str(ex))
        except Exception as ex:
            raise InvalidClientRequest(msg.get(f.IDENTIFIER.nm),
                                       msg.get(f.REQ_ID.nm)) from ex

        if needStaticValidation:
            self.doStaticValidation(cMsg)

        self.execute_hook(NodeHooks.PRE_SIG_VERIFICATION, cMsg)
        self.verifySignature(cMsg)
        logger.trace("{} received CLIENT message: {}".
                     format(self.clientstack.name, cMsg))
        return cMsg, frm

    def unpackClientMsg(self, msg, frm):
        """
        If the message is a batch message validate each message in the batch,
        otherwise add the message to the node's clientInBox.
        But node return a Nack message if View Change in progress
        :param msg: a client message
        :param frm: the name of the client that sent this `msg`
        """

        if isinstance(msg, Batch):
            for m in msg.messages:
                # This check is done since Client uses NodeStack (which can
                # send and receive BATCH) to talk to nodes but Node uses
                # ClientStack (which cannot send or receive BATCH).
                # TODO: The solution is to have both kind of stacks be able to
                # parse BATCH messages
                if m in (ZStack.pingMessage, ZStack.pongMessage):
                    continue
                m = self.clientstack.deserializeMsg(m)
                self.handleOneClientMsg((m, frm))
        else:
            msg_dict = msg.as_dict if isinstance(msg, Request) else msg
            if isinstance(msg_dict, dict):
                if self.view_changer.view_change_in_progress and self.is_request_need_quorum(msg_dict):
                    self.discard(msg_dict,
                                 reason="view change in progress",
                                 logMethod=logger.debug)
                    return
            self.postToClientInBox(msg, frm)

    def postToClientInBox(self, msg, frm):
        """
        Append the message to the node's clientInBox

        :param msg: a client message
        :param frm: the name of the node that sent this `msg`
        """
        self.clientInBox.append((msg, frm))

    async def processClientInBox(self):
        """
        Process the messages in the node's clientInBox asynchronously.
        All messages in the inBox have already been validated, including
        signature check.
        """
        while self.clientInBox:
            m = self.clientInBox.popleft()
            req, frm = m
            logger.debug("{} processing {} request {}".
                         format(self.clientstack.name, frm, req),
                         extra={"cli": True,
                                "tags": ["node-msg-processing"]})

            try:
                await self.clientMsgRouter.handle(m)
            except InvalidClientMessageException as ex:
                self.handleInvalidClientMsg(ex, m)

    # TODO: change sending format from Reject to (digest, Reject)
    # if you will use this method
    def _reject_msg(self, msg, frm, reason):
        reqKey = (msg.identifier, msg.reqId)
        reject = Reject(*reqKey,
                        reason)
        self.transmitToClient(reject, frm)

    def postPoolLedgerCaughtUp(self, **kwargs):
        self.mode = Mode.discovered
        # The node might have discovered more nodes, so see if schedule
        # election if needed.
        if isinstance(self.poolManager, TxnPoolManager):
            self.checkInstances()

        # TODO: why we do it this way?
        # Initialising node id in case where node's information was not present
        # in pool ledger at the time of starting, happens when a non-genesis
        # node starts
        self.id

    def postDomainLedgerCaughtUp(self, **kwargs):
        pass

    def postAuditLedgerCaughtUp(self, **kwargs):
        self.write_manager.on_catchup_finished()

    def preLedgerCatchUp(self, ledger_id):
        if len(self.auditLedger.uncommittedTxns) > 0:
            raise LogicError(
                '{} audit ledger has uncommitted txns before catching up ledger {}'.format(self, ledger_id))

    def postLedgerCatchUp(self, ledger_id):
        if len(self.auditLedger.uncommittedTxns) > 0:
            raise LogicError('{} audit ledger has uncommitted txns after catching up ledger {}'.format(self, ledger_id))

    def postTxnFromCatchupAddedToLedger(self, ledger_id: int, txn: Any, updateSeqNo=True):
        typ = get_type(txn)
        self.postRecvTxnFromCatchup(ledger_id, txn)
        if self.write_manager.is_valid_type(typ):
            self.write_manager.update_state(txn, isCommitted=True)
            state = self.getState(ledger_id)
            if state:
                state.commit(rootHash=state.headHash)
                if self.stateTsDbStorage and \
                        (ledger_id == DOMAIN_LEDGER_ID or ledger_id == CONFIG_LEDGER_ID):
                    timestamp = get_txn_time(txn)
                    if timestamp is not None:
                        self.stateTsDbStorage.set(timestamp, state.headHash)
                logger.trace("{} added transaction with seqNo {} to ledger {} during catchup, state root {}"
                             .format(self, get_seq_no(txn), ledger_id,
                                     state_roots_serializer.serialize(bytes(state.committedHeadHash))))
        if updateSeqNo:
            self.updateSeqNoMap([txn], ledger_id)
        self._clear_request_for_txn(ledger_id, txn)

    def _clear_request_for_txn(self, ledger_id, txn):
        req_key = get_digest(txn)
        if req_key is not None:
            self.master_replica.discard_req_key(ledger_id, req_key)
            reqState = self.requests.get(req_key, None)
            if reqState:
                if reqState.forwarded and not reqState.executed:
                    self.mark_request_as_executed(reqState.request)
                    self.requests.ordered_by_replica(reqState.request.key)
                    self.requests.free(reqState.request.key)
                    self.doneProcessingReq(req_key)
                if not reqState.forwarded:
                    self.requests.pop(req_key, None)
                    self._clean_req_from_verified(reqState.request)
                    self.doneProcessingReq(req_key)

    def postRecvTxnFromCatchup(self, ledgerId: int, txn: Any):
        if ledgerId == POOL_LEDGER_ID:
            self.poolManager.onPoolMembershipChange(txn)
        if ledgerId == DOMAIN_LEDGER_ID:
            self.post_txn_from_catchup_added_to_domain_ledger(txn)

    # TODO: should be renamed to `post_all_ledgers_caughtup`
    def allLedgersCaughtUp(self):
        if self.num_txns_caught_up_in_last_catchup() == 0:
            self.catchup_rounds_without_txns += 1
        last_txn = self.getLedger(AUDIT_LEDGER_ID).get_last_committed_txn()
        if last_txn:
            data = get_payload_data(last_txn)
            self.ledgerManager.last_caught_up_3PC = (data[AUDIT_TXN_VIEW_NO], data[AUDIT_TXN_PP_SEQ_NO])
        else:
            self.ledgerManager.last_caught_up_3PC = (0, 0)
        last_caught_up_3PC = self.ledgerManager.last_caught_up_3PC
        master_last_ordered_3PC = self.master_last_ordered_3PC
        self.mode = Mode.synced
        for replica in self.replicas.values():
            replica.on_catch_up_finished(last_caught_up_3PC,
                                         master_last_ordered_3PC)
        logger.info('{}{} caught up till {}'
                    .format(CATCH_UP_PREFIX, self, last_caught_up_3PC),
                    extra={'cli': True})
        # Replica's messages should be processed right after unstashing because the node
        # may not need a new one catchup. But in case with processing 3pc messages in
        # next looper iteration, new catchup will have already begun and unstashed 3pc
        # messages will stash again.
        # TODO: Divide different catchup iterations for different looper iterations. And remove this call after.
        if self.view_change_in_progress:
            self._process_replica_messages()

        # More than one catchup may be needed during the current ViewChange protocol
        # TODO: separate view change and catchup logic
        if self.is_catchup_needed():
            logger.info('{} needs to catchup again'.format(self))
            self.start_catchup()
        else:
            logger.info('{}{} does not need any more catchups'
                        .format(CATCH_UP_PREFIX, self),
                        extra={'cli': True})

            self.no_more_catchups_needed()

            if self.view_change_in_progress:
                self.view_changer.on_catchup_complete()
            else:
                self.select_primaries_on_catchup_complete()

    def select_primaries_on_catchup_complete(self):
        # Select primaries after usual catchup (not view change)
        ledger = self.getLedger(AUDIT_LEDGER_ID)
        self.backup_instance_faulty_processor.restore_replicas()
        self.drop_primaries()
        if len(ledger) == 0:
            self.select_primaries()
        else:
            # Emulate view change start
            self.view_changer.previous_view_no = self.viewNo
            self.viewNo = get_payload_data(ledger.get_last_committed_txn())[AUDIT_TXN_VIEW_NO]
            self.view_changer.previous_master_primary = self.master_primary_name
            self.view_changer.set_defaults()

            self.primaries = self._get_last_audited_primaries()
            if len(self.replicas) != len(self.primaries):
                logger.error('Audit ledger has inconsistent number of nodes. '
                             'Node primaries = {}'.format(self.primaries))
            if any(p not in self.nodeReg for p in self.primaries):
                logger.error('Audit ledger has inconsistent names of primaries. '
                             'Node primaries = {}'.format(self.primaries))
            # Similar functionality to select_primaries
            for instance_id, replica in self.replicas.items():
                if instance_id == 0:
                    self.start_participating()
                replica.primaryChanged(
                    Replica.generateName(self.primaries[instance_id], instance_id))
                self.primary_selected(instance_id)

        # Primary propagation
        last_sent_pp_seq_no_restored = False
        for replica in self.replicas.values():
            replica.on_propagate_primary_done()
        if self.view_changer.previous_view_no == 0:
            last_sent_pp_seq_no_restored = \
                self.last_sent_pp_store_helper.try_restore_last_sent_pp_seq_no()
        if not last_sent_pp_seq_no_restored:
            self.last_sent_pp_store_helper.erase_last_sent_pp_seq_no()

        # Emulate view_change ending
        self.on_view_propagated()

    def _get_last_audited_primaries(self):
        audit = self.getLedger(AUDIT_LEDGER_ID)
        last_txn = audit.get_last_committed_txn()
        last_txn_prim_value = get_payload_data(last_txn)[AUDIT_TXN_PRIMARIES]

        if isinstance(last_txn_prim_value, int):
            seq_no = get_seq_no(last_txn) - last_txn_prim_value
            last_txn_prim_value = get_payload_data(audit.getBySeqNo(seq_no))[AUDIT_TXN_PRIMARIES]

        return last_txn_prim_value

    def is_catchup_needed(self) -> bool:
        # More than one catchup may be needed during the current ViewChange protocol
        if self.view_change_in_progress:
            return self.is_catchup_needed_during_view_change()

        # If we already have audit ledger we don't need any more catch-ups
        if self.auditLedger.size > 0:
            return False

        # Do a catchup until there are no more new transactions
        return self.num_txns_caught_up_in_last_catchup() > 0

    def is_catchup_needed_during_view_change(self) -> bool:
        """
        Check if received a quorum of view change done messages and if yes
        check if caught up till the
        Check if all requests ordered till last prepared certificate
        Check if last catchup resulted in no txns
        """
        if self.caught_up_for_current_view():
            logger.info('{} is caught up for the current view {}'.format(self, self.viewNo))
            return False
        logger.info('{} is not caught up for the current view {}'.format(self, self.viewNo))

        if self.num_txns_caught_up_in_last_catchup() == 0:
            if self.has_ordered_till_last_prepared_certificate():
                logger.info('{} ordered till last prepared certificate'.format(self))
                return False

        if self.is_catch_up_limit(self.config.MIN_TIMEOUT_CATCHUPS_DONE_DURING_VIEW_CHANGE):
            # No more 3PC messages will be processed since maximum catchup
            # rounds have been done
            self.master_replica.last_prepared_before_view_change = None
            return False

        return True

    def caught_up_for_current_view(self) -> bool:
        if not self.view_changer._hasViewChangeQuorum:
            logger.info('{} does not have view change quorum for view {}'.format(self, self.viewNo))
            return False
        vc = self.view_changer.get_sufficient_same_view_change_done_messages()
        if not vc:
            logger.info('{} does not have acceptable ViewChangeDone for view {}'.format(self, self.viewNo))
            return False
        ledger_info = vc[1]
        for lid, size, root_hash in ledger_info:
            ledger = self.ledgerManager.ledgerRegistry[lid].ledger
            if size == 0:
                continue
            if ledger.size < size:
                return False
            if ledger.hashToStr(
                    ledger.tree.merkle_tree_hash(0, size)) != root_hash:
                return False
        return True

    def has_ordered_till_last_prepared_certificate(self) -> bool:
        lst = self.master_replica.last_prepared_before_view_change
        if lst is None:
            return True
        return compare_3PC_keys(lst, self.master_replica.last_ordered_3pc) >= 0

    def is_catch_up_limit(self, timeout: float):
        ts_since_catch_up_start = time.perf_counter() - self._catch_up_start_ts
        if ts_since_catch_up_start >= timeout:
            logger.info('{} has completed {} catchup rounds for {} seconds'.
                        format(self, self.catchup_rounds_without_txns, ts_since_catch_up_start))
            return True
        return False

    def num_txns_caught_up_in_last_catchup(self) -> int:
        count = self.ledgerManager._node_leecher.num_txns_caught_up_in_last_catchup()
        logger.info('{} caught up to {} txns in the last catchup'.format(self, count))
        return count

    def no_more_catchups_needed(self):
        # This method is called when no more catchups needed
        self._catch_up_start_ts = 0

    def getLedger(self, ledgerId) -> Ledger:
        try:
            return self.ledgerManager.ledgerRegistry[ledgerId].ledger
        except KeyError:
            raise KeyError("Invalid ledger type: {}".format(ledgerId))

    def getState(self, ledgerId) -> PruningState:
        return self.states.get(ledgerId)

    def post_txn_from_catchup_added_to_domain_ledger(self, txn):
        if get_type(txn) == NYM:
            self.addNewRole(txn)

    def getLedgerStatus(self, ledgerId: int):
        if ledgerId == POOL_LEDGER_ID and not self.poolLedger:
            # Since old style nodes don't know have pool ledger
            return None
        if ledgerId not in self.ledger_ids:
            return None
        return self.build_ledger_status(ledgerId)

    def sendLedgerStatus(self, nodeName: str, ledgerId: int):
        ledgerStatus = self.getLedgerStatus(ledgerId)
        if ledgerStatus:
            self.sendToNodes(ledgerStatus, [nodeName])
        else:
            logger.info("{} not sending ledger {} status to {} as it is null".format(self, ledgerId, nodeName))

    def send_ledger_status_to_client(self, lid, txn_s_n, v, p, merkle, protocol, client):
        ls = LedgerStatus(lid, txn_s_n, v, p, merkle, protocol)
        self.transmitToClient(ls, client)

    def _get_manager_for_txn_type(self, txn_type):
        if self.write_manager.is_valid_type(txn_type):
            return self.write_manager
        if self.read_manager.is_valid_type(txn_type):
            return self.read_manager
        if self.action_manager.is_valid_type(txn_type):
            return self.action_manager
        return None

    def doStaticValidation(self, request: Request):
        identifier, req_id, operation = request.identifier, request.reqId, request.operation
        if TXN_TYPE not in operation:
            raise InvalidClientRequest(identifier, req_id)

        if operation[TXN_TYPE] != GET_TXN:
            # GET_TXN is generic, needs no request handler
            txn_type = operation[TXN_TYPE]
            req_manager = self._get_manager_for_txn_type(txn_type)
            if req_manager is None:
                raise InvalidClientRequest(identifier, req_id, 'invalid {}: {}'.
                                           format(TXN_TYPE, operation[TXN_TYPE]))
            else:
                req_manager.static_validation(request)

    # TODO hooks might need pp_time as well
    def doDynamicValidation(self, request: Request, req_pp_time: int):
        """
        State based validation
        """
        # Digest validation
        # TODO implicit caller's context: request is processed by (master) replica
        # as part of PrePrepare 3PC batch
        ledger_id, seq_no = self.seqNoDB.get_by_payload_digest(request.payload_digest)
        if ledger_id is not None and seq_no is not None:
            raise SuspiciousPrePrepare('Trying to order already ordered request')

        ledger = self.getLedger(self.ledger_id_for_request(request))
        for txn in ledger.uncommittedTxns:
            if get_payload_digest(txn) == request.payload_digest:
                raise SuspiciousPrePrepare('Trying to order already ordered request')

        # specific validation for the request txn type
        operation = request.operation
        req_manager = self._get_manager_for_txn_type(txn_type=operation[TXN_TYPE])
        # TAA validation
        # For now, we need to call taa_validation not from dynamic_validation because
        # req_pp_time is required
        req_manager.do_taa_validation(request, req_pp_time, self.config)
        req_manager.dynamic_validation(request)

    def applyReq(self, request: Request, cons_time: int):
        """
        Apply request to appropriate ledger and state. `cons_time` is the
        UTC epoch at which consensus was reached.
        """
        req_manager = self._get_manager_for_txn_type(txn_type=request.operation[TXN_TYPE])
        req_manager.apply_request(request, cons_time)

    def apply_stashed_reqs(self, three_pc_batch):
        request_ids = three_pc_batch.valid_digests
        requests = []
        for req_key in request_ids:
            if req_key in self.requests:
                req = self.requests[req_key].finalised
            else:
                logger.warning("Could not apply stashed requests due to non-existent requests")
                return
            _, seq_no = self.seqNoDB.get_by_payload_digest(req.payload_digest)
            if seq_no is None:
                requests.append(req)
        self.apply_reqs(requests, three_pc_batch)

    def apply_reqs(self, requests, three_pc_batch: ThreePcBatch):
        for req in requests:
            self.applyReq(req, three_pc_batch.pp_time)
        self.onBatchCreated(three_pc_batch)

    def handle_request_if_forced(self, request: Request, frm):
        if request.isForced():
            req_manager = self._get_manager_for_txn_type(txn_type=request.operation[TXN_TYPE])
            try:
                req_manager.dynamic_validation(request)
            except Exception as e:
                self.transmitToClient(RequestNack(request.identifier, request.reqId, str(e)), frm)
                self.doneProcessingReq(request.key)
                return False

            req_manager.apply_forced_request(request)
        return True

    @measure_time(MetricsName.PROCESS_REQUEST_TIME)
    def processRequest(self, request: Request, frm: str):
        """
        Handle a REQUEST from the client.
        If the request has already been executed, the node re-sends the reply to
        the client. Otherwise, the node acknowledges the client request, adds it
        to its list of client requests, and sends a PROPAGATE to the
        remaining nodes.

        :param request: the REQUEST from the client
        :param frm: the name of the client that sent this REQUEST
        """
        logger.debug("{} received client request: {} from {}".
                     format(self.name, request, frm))
        self.nodeRequestSpikeMonitorData['accum'] += 1

        # TODO: What if client sends requests with same request id quickly so
        # before reply for one is generated, the other comes. In that
        # case we need to keep track of what requests ids node has seen
        # in-memory and once request with a particular request id is processed,
        # it should be removed from that in-memory DS.

        # If request is already processed(there is a reply for the
        # request in
        # the node's transaction store then return the reply from the
        # transaction store)
        # TODO: What if the reply was a REQNACK? Its not gonna be found in the
        # replies.

        txn_type = request.operation[TXN_TYPE]

        if self.is_action(txn_type):
            self.process_action(request, frm)

        elif txn_type == GET_TXN:
            self.handle_get_txn_req(request, frm)
            self.total_read_request_number += 1

        elif self.is_query(txn_type):
            self.process_query(request, frm)
            self.total_read_request_number += 1

        elif self.can_write_txn(txn_type):
            reply = self.getReplyFromLedgerForRequest(request)
            if reply:
                logger.debug("{} returning reply from already processed "
                             "REQUEST: {}".format(self, request))
                self.transmitToClient(reply, frm)
                return

            # If the node is not already processing the request
            if not self.isProcessingReq(request.key):
                self.startedProcessingReq(request.key, frm)
                # forced request should be processed before consensus
                handle_result = self.handle_request_if_forced(request, frm)
                if not handle_result:
                    return

            # If not already got the propagate request(PROPAGATE) for the
            # corresponding client request(REQUEST)
            self.recordAndPropagate(request, frm)
            self.send_ack_to_client((request.identifier, request.reqId), frm)

        else:
            raise InvalidClientRequest(
                request.identifier,
                request.reqId,
                'Pool is in readonly mode, try again in 60 seconds')

    def is_query(self, txn_type) -> bool:
        # Does the transaction type correspond to a read?
        return self.read_manager.is_valid_type(txn_type)

    def is_action(self, txn_type) -> bool:
        return self.action_manager.is_valid_type(txn_type)

    def can_write_txn(self, txn_type):
        return True

    def process_query(self, request: Request, frm: str):
        # Process a read request from client
        try:
            self.read_manager.static_validation(request)
            self.send_ack_to_client((request.identifier, request.reqId), frm)
        except Exception as ex:
            self.send_nack_to_client((request.identifier, request.reqId),
                                     str(ex), frm)
        result = self.read_manager.get_result(request)
        self.transmitToClient(Reply(result), frm)

    def process_action(self, request, frm):
        # Process an execute action request
        self.send_ack_to_client((request.identifier, request.reqId), frm)
        try:
            self.action_manager.dynamic_validation(request)
            result = self.action_manager.process_action(request)
            self.transmitToClient(Reply(result), frm)
        except Exception as ex:
            self.transmitToClient(Reject(request.identifier,
                                         request.reqId,
                                         str(ex)), frm)

    # noinspection PyUnusedLocal
    @measure_time(MetricsName.PROCESS_PROPAGATE_TIME)
    def processPropagate(self, msg: Propagate, frm):
        """
        Process one propagateRequest sent to this node asynchronously

        - If this propagateRequest hasn't been seen by this node, then broadcast
        it to all nodes after verifying the the signature.
        - Add the client to blacklist if its signature is invalid

        :param msg: the propagateRequest
        :param frm: the name of the node which sent this `msg`
        """
        logger.debug("{} received propagated request: {}".
                     format(self.name, msg))

        request = TxnUtilConfig.client_request_class(**msg.request)

        clientName = msg.senderClient

        if not self.isProcessingReq(request.key):
            ledger_id, seq_no = self.seqNoDB.get_by_payload_digest(request.payload_digest)
            if ledger_id is not None and seq_no is not None:
                self._clean_req_from_verified(request)
                logger.debug("{} ignoring propagated request {} "
                             "since it has been already ordered"
                             .format(self.name, msg))
                return

            self.startedProcessingReq(request.key, clientName)
            # forced request should be processed before consensus
            handle_result = self.handle_request_if_forced(request, clientName)
            if not handle_result:
                return

        else:
            if clientName is not None and \
                    not self.is_sender_known_for_req(request.key):
                # Since some propagates might not include the client name
                self.set_sender_for_req(request.key,
                                        clientName)

        self.requests.add_propagate(request, frm)

        self.propagate(request, clientName)
        self.tryForwarding(request)

    def startedProcessingReq(self, key, frm):
        self.requestSender[key] = frm

    def isProcessingReq(self, key) -> bool:
        return key in self.requestSender

    def doneProcessingReq(self, key):
        if key in self.requestSender:
            self.requestSender.pop(key)

    def is_sender_known_for_req(self, key):
        return self.requestSender.get(key) is not None

    def set_sender_for_req(self, key, frm):
        self.requestSender[key] = frm

    def send_ack_to_client(self, req_key, to_client):
        self.transmitToClient(RequestAck(*req_key), to_client)

    def send_nack_to_client(self, req_key, reason, to_client):
        self.transmitToClient(RequestNack(*req_key, reason), to_client)

    def handle_get_txn_req(self, request: Request, frm: str):
        """
        Handle GET_TXN request
        """
        ledger_id = request.operation.get(f.LEDGER_ID.nm, DOMAIN_LEDGER_ID)
        if ledger_id not in self.ledger_ids:
            self.send_nack_to_client((request.identifier, request.reqId),
                                     'Invalid ledger id {}'.format(ledger_id),
                                     frm)
            return

        seq_no = request.operation.get(DATA)
        self.send_ack_to_client((request.identifier, request.reqId), frm)
        ledger = self.getLedger(ledger_id)

        try:
            txn = self.getReplyFromLedger(ledger, seq_no)
        except KeyError:
            txn = None

        if txn is None:
            logger.debug(
                "{} can not handle GET_TXN request: ledger doesn't "
                "have txn with seqNo={}".format(self, str(seq_no)))

        result = {
            f.IDENTIFIER.nm: request.identifier,
            f.REQ_ID.nm: request.reqId,
            TXN_TYPE: request.operation[TXN_TYPE],
            DATA: None
        }

        if txn:
            result[DATA] = txn.result
            result[f.SEQ_NO.nm] = get_seq_no(txn.result)

        self.transmitToClient(Reply(result), frm)

    @measure_time(MetricsName.PROCESS_ORDERED_TIME)
    def processOrdered(self, ordered: Ordered):
        """
        Execute ordered request

        :param ordered: an ordered request
        :return: whether executed
        """

        if ordered.instId not in self.instances.ids:
            logger.warning('{} got ordered request for instance {} which '
                           'does not exist'.format(self, ordered.instId))
            return False

        if ordered.instId != self.instances.masterId:
            # Requests from backup replicas are not executed
            logger.trace("{} got ordered requests from backup replica {}"
                         .format(self, ordered.instId))
            with self.metrics.measure_time(MetricsName.MONITOR_REQUEST_ORDERED_TIME):
                self.monitor.requestOrdered(ordered.valid_reqIdr + ordered.invalid_reqIdr,
                                            ordered.instId,
                                            self.requests,
                                            byMaster=False)
            return False

        logger.trace("{} got ordered requests from master replica"
                     .format(self))

        logger.debug("{} executing Ordered batch {} {} of {} requests; state root {}; txn root {}"
                     .format(self.name,
                             ordered.viewNo,
                             ordered.ppSeqNo,
                             len(ordered.valid_reqIdr),
                             ordered.stateRootHash,
                             ordered.txnRootHash))

        three_pc_batch = ThreePcBatch.from_ordered(ordered)
        if self.db_manager.ledgers[AUDIT_LEDGER_ID].uncommittedRootHash is None:
            # if we order request during view change
            # in between catchup rounds, then the 3PC batch will not be applied,
            # since it was reverted before catchup started, and only COMMITs were
            # processed in between catchup that led to this ORDERED msg
            logger.info("{} applying stashed requests for batch {} {} of {} requests; state root {}; txn root {}"
                        .format(self.name,
                                three_pc_batch.view_no,
                                three_pc_batch.pp_seq_no,
                                len(three_pc_batch.valid_digests),
                                three_pc_batch.state_root,
                                three_pc_batch.txn_root))

            self.apply_stashed_reqs(three_pc_batch)

        self.executeBatch(three_pc_batch,
                          ordered.valid_reqIdr,
                          ordered.invalid_reqIdr,
                          ordered.auditTxnRootHash)

        with self.metrics.measure_time(MetricsName.MONITOR_REQUEST_ORDERED_TIME):
            self.monitor.requestOrdered(ordered.valid_reqIdr + ordered.invalid_reqIdr,
                                        ordered.instId,
                                        self.requests,
                                        byMaster=True)

        return True

    def force_process_ordered(self):
        """
        Take any messages from replica that have been ordered and process
        them, this should be done rarely, like before catchup starts
        so a more current LedgerStatus can be sent.
        can be called either
        1. when node is participating, this happens just before catchup starts
        so the node can have the latest ledger status or
        2. when node is not participating but a round of catchup is about to be
        started, here is forces all the replica ordered messages to be appended
        to the stashed ordered requests and the stashed ordered requests are
        processed with appropriate checks
        """

        for instance_id, messages in self.replicas.take_ordereds_out_of_turn():
            num_processed = 0
            for message in messages:
                self.try_processing_ordered(message)
                num_processed += 1
            logger.info('{} processed {} Ordered batches for instance {} '
                        'before starting catch up'
                        .format(self, num_processed, instance_id))

    def try_processing_ordered(self, msg):
        if self.master_replica.validator.can_order():
            self.processOrdered(msg)
        else:
            logger.warning("{} can not process Ordered message {} since mode is {}".format(self, msg, self.mode))

    def processEscalatedException(self, ex):
        """
        Process an exception escalated from a Replica
        """
        if isinstance(ex, SuspiciousNode):
            self.reportSuspiciousNodeEx(ex)
        else:
            raise RuntimeError("unhandled replica-escalated exception") from ex

    def _update_new_ordered_reqs_count(self):
        """
        Checks if any requests have been ordered since last performance check
        and updates the performance check data store if needed.
        :return: True if new ordered requests, False otherwise
        """
        last_num_ordered = self._last_performance_check_data.get('num_ordered')
        num_ordered = sum(num for num, _ in self.monitor.numOrderedRequests.values())
        if num_ordered != last_num_ordered:
            self._last_performance_check_data['num_ordered'] = num_ordered
            return True
        else:
            return False

    def report_gc_stats(self):
        obj_tree = GcObjectTree()
        obj_tree.report_top_obj_types()
        obj_tree.report_top_collections()
        obj_tree.cleanup()

    def flush_metrics(self):
        # Flush accumulated should always be done to avoid numeric overflow in accumulators
        self.metrics.flush_accumulated()
        if self.config.METRICS_COLLECTOR_TYPE is None:
            return

        ram_by_process = psutil.Process().memory_info()
        self.metrics.add_event(MetricsName.AVAILABLE_RAM_SIZE, psutil.virtual_memory().available)
        self.metrics.add_event(MetricsName.NODE_RSS_SIZE, ram_by_process.rss)
        self.metrics.add_event(MetricsName.NODE_VMS_SIZE, ram_by_process.vms)
        self.metrics.add_event(MetricsName.CONNECTED_CLIENTS_NUM, self.clientstack.connected_clients_num)
        self.metrics.add_event(MetricsName.GC_TRACKED_OBJECTS, len(gc.get_objects()))

        self.metrics.add_event(MetricsName.REQUEST_QUEUE_SIZE, len(self.requests))
        self.metrics.add_event(MetricsName.FINALISED_REQUEST_QUEUE_SIZE, self.requests.finalised_count)
        self.metrics.add_event(MetricsName.MONITOR_REQUEST_QUEUE_SIZE, len(self.monitor.requestTracker))
        self.metrics.add_event(MetricsName.MONITOR_UNORDERED_REQUEST_QUEUE_SIZE,
                               len(self.monitor.requestTracker.unordered()))
        self.metrics.add_event(MetricsName.PROPAGATES_PHASE_REQ_TIMEOUTS, self.propagates_phase_req_timeouts)
        self.metrics.add_event(MetricsName.ORDERING_PHASE_REQ_TIMEOUTS, self.ordering_phase_req_timeouts)

        if self.view_changer is not None:
            self.metrics.add_event(MetricsName.CURRENT_VIEW, self.viewNo)
            self.metrics.add_event(MetricsName.VIEW_CHANGE_IN_PROGRESS, int(self.view_changer.view_change_in_progress))

        self.metrics.add_event(MetricsName.NODE_STATUS, int(self.mode) if self.mode is not None else 0)
        self.metrics.add_event(MetricsName.CONNECTED_NODES_NUM, self.connectedNodeCount)
        self.metrics.add_event(MetricsName.BLACKLISTED_NODES_NUM, len(self.blacklistedNodes))
        self.metrics.add_event(MetricsName.REPLICA_COUNT, self.replicas.num_replicas)

        self.metrics.add_event(MetricsName.POOL_LEDGER_SIZE, self.poolLedger.size)
        self.metrics.add_event(MetricsName.DOMAIN_LEDGER_SIZE, self.domainLedger.size)
        self.metrics.add_event(MetricsName.CONFIG_LEDGER_SIZE, self.configLedger.size)

        self.metrics.add_event(MetricsName.POOL_LEDGER_UNCOMMITTED_SIZE, len(self.poolLedger.uncommittedTxns))
        self.metrics.add_event(MetricsName.DOMAIN_LEDGER_UNCOMMITTED_SIZE, len(self.domainLedger.uncommittedTxns))
        self.metrics.add_event(MetricsName.CONFIG_LEDGER_UNCOMMITTED_SIZE, len(self.configLedger.uncommittedTxns))

        # Collections metrics
        def sum_for_values(obj):
            # We don't want to get 0 if we have huge dictionary of empty queues, hence +1
            return sum(len(v) + 1 for v in obj.values())

        self.metrics.add_event(MetricsName.NODE_STACK_RX_MSGS, len(self.nodestack.rxMsgs))
        self.metrics.add_event(MetricsName.CLIENT_STACK_RX_MSGS, len(self.clientstack.rxMsgs))

        self.metrics.add_event(MetricsName.NODE_ACTION_QUEUE, len(self.actionQueue))
        self.metrics.add_event(MetricsName.NODE_AQ_STASH, len(self.aqStash))
        self.metrics.add_event(MetricsName.NODE_REPEATING_ACTIONS, len(self.repeatingActions))
        self.metrics.add_event(MetricsName.NODE_SCHEDULED, len(self.scheduled))

        self.metrics.add_event(MetricsName.NODE_REQUESTED_PROPAGATES_FOR, len(self.requested_propagates_for))

        self.metrics.add_event(MetricsName.TIMER_QUEUE_SIZE, self.timer.queue_size())

        self.metrics.add_event(MetricsName.VIEW_CHANGER_INBOX, len(self.view_changer.inBox))
        self.metrics.add_event(MetricsName.VIEW_CHANGER_OUTBOX, len(self.view_changer.outBox))
        self.metrics.add_event(MetricsName.VIEW_CHANGER_NEXT_VIEW_INDICATIONS,
                               len(self.view_changer._next_view_indications))
        self.metrics.add_event(MetricsName.VIEW_CHANGER_VIEW_CHANGE_DONE, len(self.view_changer._view_change_done))

        self.metrics.add_event(MetricsName.MSGS_FOR_FUTURE_REPLICAS, len(self.msgsForFutureReplicas))
        self.metrics.add_event(MetricsName.MSGS_TO_VIEW_CHANGER, len(self.msgsToViewChanger))
        self.metrics.add_event(MetricsName.REQUEST_SENDER, len(self.requestSender))

        self.metrics.add_event(MetricsName.MSGS_FOR_FUTURE_VIEWS, len(self.msgsForFutureViews))

        self.metrics.add_event(MetricsName.LEDGERMANAGER_POOL_UNCOMMITEDS, len(self.getLedger(0).uncommittedTxns))
        self.metrics.add_event(MetricsName.LEDGERMANAGER_DOMAIN_UNCOMMITEDS, len(self.getLedger(1).uncommittedTxns))
        self.metrics.add_event(MetricsName.LEDGERMANAGER_CONFIG_UNCOMMITEDS, len(self.getLedger(2).uncommittedTxns))

        # REPLICAS
        self.metrics.add_event(MetricsName.REPLICA_OUTBOX_MASTER, len(self.master_replica.outBox))
        self.metrics.add_event(MetricsName.REPLICA_INBOX_MASTER, len(self.master_replica.inBox))
        self.metrics.add_event(MetricsName.REPLICA_INBOX_STASH_MASTER, len(self.master_replica.inBoxStash))
        self.metrics.add_event(MetricsName.REPLICA_PREPREPARES_PENDING_FIN_REQS_MASTER,
                               len(self.master_replica.prePreparesPendingFinReqs))
        self.metrics.add_event(MetricsName.REPLICA_PREPREPARES_PENDING_PREVPP_MASTER,
                               len(self.master_replica.prePreparesPendingPrevPP))
        self.metrics.add_event(MetricsName.REPLICA_PREPARES_WAITING_FOR_PREPREPARE_MASTER,
                               sum_for_values(self.master_replica.preparesWaitingForPrePrepare))
        self.metrics.add_event(MetricsName.REPLICA_COMMITS_WAITING_FOR_PREPARE_MASTER,
                               sum_for_values(self.master_replica.commitsWaitingForPrepare))
        self.metrics.add_event(MetricsName.REPLICA_SENT_PREPREPARES_MASTER, len(self.master_replica.sentPrePrepares))
        self.metrics.add_event(MetricsName.REPLICA_PREPREPARES_MASTER, len(self.master_replica.prePrepares))
        self.metrics.add_event(MetricsName.REPLICA_PREPARES_MASTER, len(self.master_replica.prepares))
        self.metrics.add_event(MetricsName.REPLICA_COMMITS_MASTER, len(self.master_replica.commits))
        self.metrics.add_event(MetricsName.REPLICA_PRIMARYNAMES_MASTER, len(self.master_replica.primaryNames))
        self.metrics.add_event(MetricsName.REPLICA_STASHED_OUT_OF_ORDER_COMMITS_MASTER,
                               sum_for_values(self.master_replica.stashed_out_of_order_commits))
        self.metrics.add_event(MetricsName.REPLICA_CHECKPOINTS_MASTER,
                               len(self.master_replica._consensus_data.checkpoints))
        self.metrics.add_event(MetricsName.REPLICA_STASHED_RECVD_CHECKPOINTS_MASTER,
                               sum_for_values(self.master_replica._checkpointer._stashed_recvd_checkpoints))
        self.metrics.add_event(MetricsName.REPLICA_STASHING_WHILE_OUTSIDE_WATERMARKS_MASTER,
                               self.master_replica.stasher.num_stashed_watermarks)
        self.metrics.add_event(MetricsName.REPLICA_REQUEST_QUEUES_MASTER,
                               sum_for_values(self.master_replica.requestQueues))
        self.metrics.add_event(MetricsName.REPLICA_BATCHES_MASTER, len(self.master_replica.batches))
        self.metrics.add_event(MetricsName.REPLICA_REQUESTED_PRE_PREPARES_MASTER,
                               len(self.master_replica.requested_pre_prepares))
        self.metrics.add_event(MetricsName.REPLICA_REQUESTED_PREPARES_MASTER,
                               len(self.master_replica.requested_prepares))
        self.metrics.add_event(MetricsName.REPLICA_REQUESTED_COMMITS_MASTER, len(self.master_replica.requested_commits))
        self.metrics.add_event(MetricsName.REPLICA_PRE_PREPARES_STASHED_FOR_INCORRECT_TIME_MASTER,
                               len(self.master_replica.pre_prepares_stashed_for_incorrect_time))

        self.metrics.add_event(MetricsName.REPLICA_ACTION_QUEUE_MASTER, len(self.master_replica.actionQueue))
        self.metrics.add_event(MetricsName.REPLICA_AQ_STASH_MASTER, len(self.master_replica.aqStash))
        self.metrics.add_event(MetricsName.REPLICA_REPEATING_ACTIONS_MASTER, len(self.master_replica.repeatingActions))
        self.metrics.add_event(MetricsName.REPLICA_SCHEDULED_MASTER, len(self.master_replica.scheduled))
        self.metrics.add_event(MetricsName.REPLICA_STASHED_CATCHUP_MASTER,
                               self.master_replica.stasher.num_stashed_catchup)
        self.metrics.add_event(MetricsName.REPLICA_STASHED_FUTURE_VIEW_MASTER,
                               self.master_replica.stasher.num_stashed_future_view)
        self.metrics.add_event(MetricsName.REPLICA_STASHED_WATERMARKS_MASTER,
                               self.master_replica.stasher.num_stashed_watermarks)

        def sum_for_backups(field):
            return sum(len(getattr(r, field)) for r in self.replicas._replicas.values() if r is not self.master_replica)

        def sum_for_backups_data(field):
            return sum(len(getattr(r._consensus_data, field)) for r in self.replicas._replicas.values() if r is not self.master_replica)

        def sum_for_values_for_backups(field):
            return sum(sum_for_values(getattr(r, field))
                       for r in self.replicas._replicas.values() if r is not self.master_replica)

        def sum_for_values_for_backups_checkpointer(field):
            return sum(sum_for_values(getattr(r._checkpointer, field))
                       for r in self.replicas._replicas.values() if r is not self.master_replica)

        self.metrics.add_event(MetricsName.REPLICA_OUTBOX_BACKUP, sum_for_backups('outBox'))
        self.metrics.add_event(MetricsName.REPLICA_INBOX_BACKUP, sum_for_backups('inBox'))
        self.metrics.add_event(MetricsName.REPLICA_INBOX_STASH_BACKUP, sum_for_backups('inBoxStash'))
        self.metrics.add_event(MetricsName.REPLICA_PREPREPARES_PENDING_FIN_REQS_BACKUP,
                               sum_for_backups('prePreparesPendingFinReqs'))
        self.metrics.add_event(MetricsName.REPLICA_PREPREPARES_PENDING_PREVPP_BACKUP,
                               sum_for_backups('prePreparesPendingPrevPP'))
        self.metrics.add_event(MetricsName.REPLICA_PREPARES_WAITING_FOR_PREPREPARE_BACKUP,
                               sum_for_values_for_backups('preparesWaitingForPrePrepare'))
        self.metrics.add_event(MetricsName.REPLICA_COMMITS_WAITING_FOR_PREPARE_BACKUP,
                               sum_for_values_for_backups('commitsWaitingForPrepare'))
        self.metrics.add_event(MetricsName.REPLICA_SENT_PREPREPARES_BACKUP, sum_for_backups('sentPrePrepares'))
        self.metrics.add_event(MetricsName.REPLICA_PREPREPARES_BACKUP, sum_for_backups('prePrepares'))
        self.metrics.add_event(MetricsName.REPLICA_PREPARES_BACKUP, sum_for_backups('prepares'))
        self.metrics.add_event(MetricsName.REPLICA_COMMITS_BACKUP, sum_for_backups('commits'))
        self.metrics.add_event(MetricsName.REPLICA_PRIMARYNAMES_BACKUP, sum_for_backups('primaryNames'))
        self.metrics.add_event(MetricsName.REPLICA_STASHED_OUT_OF_ORDER_COMMITS_BACKUP,
                               sum_for_values_for_backups('stashed_out_of_order_commits'))
        self.metrics.add_event(MetricsName.REPLICA_CHECKPOINTS_BACKUP, sum_for_backups_data('checkpoints'))
        self.metrics.add_event(MetricsName.REPLICA_STASHED_RECVD_CHECKPOINTS_BACKUP,
                               sum_for_values_for_backups_checkpointer('_stashed_recvd_checkpoints'))
        self.metrics.add_event(MetricsName.REPLICA_STASHING_WHILE_OUTSIDE_WATERMARKS_BACKUP,
                               sum(r.stasher.num_stashed_watermarks for r in self.replicas.values()))
        self.metrics.add_event(MetricsName.REPLICA_REQUEST_QUEUES_BACKUP,
                               sum_for_values_for_backups('requestQueues'))
        self.metrics.add_event(MetricsName.REPLICA_BATCHES_BACKUP, sum_for_backups('batches'))
        self.metrics.add_event(MetricsName.REPLICA_REQUESTED_PRE_PREPARES_BACKUP,
                               sum_for_backups('requested_pre_prepares'))
        self.metrics.add_event(MetricsName.REPLICA_REQUESTED_PREPARES_BACKUP, sum_for_backups('requested_prepares'))
        self.metrics.add_event(MetricsName.REPLICA_REQUESTED_COMMITS_BACKUP, sum_for_backups('requested_commits'))
        self.metrics.add_event(MetricsName.REPLICA_PRE_PREPARES_STASHED_FOR_INCORRECT_TIME_BACKUP,
                               sum_for_backups('pre_prepares_stashed_for_incorrect_time'))
        self.metrics.add_event(MetricsName.REPLICA_ACTION_QUEUE_BACKUP, sum_for_backups('actionQueue'))
        self.metrics.add_event(MetricsName.REPLICA_AQ_STASH_BACKUP, sum_for_backups('aqStash'))
        self.metrics.add_event(MetricsName.REPLICA_REPEATING_ACTIONS_BACKUP, sum_for_backups('repeatingActions'))
        self.metrics.add_event(MetricsName.REPLICA_SCHEDULED_BACKUP, sum_for_backups('scheduled'))

        # Stashed msgs
        def sum_stashed_for_backups(field):
            return sum(getattr(r.stasher, field)
                       for r in self.replicas._replicas.values() if r is not self.master_replica)

        self.metrics.add_event(MetricsName.REPLICA_STASHED_CATCHUP_BACKUP,
                               sum_stashed_for_backups('num_stashed_catchup'))
        self.metrics.add_event(MetricsName.REPLICA_STASHED_FUTURE_VIEW_BACKUP,
                               sum_stashed_for_backups('num_stashed_future_view'))
        self.metrics.add_event(MetricsName.REPLICA_STASHED_WATERMARKS_BACKUP,
                               sum_stashed_for_backups('num_stashed_watermarks'))

        def store_rocksdb_metrics(name, storage):
            if not hasattr(storage, '_db'):
                return
            if not hasattr(storage._db, 'get_property'):
                return
            self.metrics.add_event(name, int(storage._db.get_property(b"rocksdb.estimate-table-readers-mem")))
            self.metrics.add_event(name + 1, int(storage._db.get_property(b"rocksdb.num-immutable-mem-table")))
            self.metrics.add_event(name + 2, int(storage._db.get_property(b"rocksdb.cur-size-all-mem-tables")))

        if hasattr(self, 'idrCache'):
            store_rocksdb_metrics(MetricsName.STORAGE_IDR_CACHE_READERS, self.idrCache._keyValueStorage)

        if hasattr(self, 'attributeStore'):
            store_rocksdb_metrics(MetricsName.STORAGE_ATTRIBUTE_STORE_READERS, self.attributeStore._keyValueStorage)

        store_rocksdb_metrics(MetricsName.STORAGE_POOL_STATE_READERS, self.states.get(0)._kv)
        store_rocksdb_metrics(MetricsName.STORAGE_DOMAIN_STATE_READERS, self.states.get(1)._kv)
        store_rocksdb_metrics(MetricsName.STORAGE_CONFIG_STATE_READERS, self.states.get(2)._kv)
        store_rocksdb_metrics(MetricsName.STORAGE_BLS_BFT_READERS, self.bls_bft.bls_store._kvs)
        store_rocksdb_metrics(MetricsName.STORAGE_SEQ_NO_READERS, self.seqNoDB._keyValueStorage)
        if self.config.METRICS_COLLECTOR_TYPE == 'kv':
            store_rocksdb_metrics(MetricsName.STORAGE_METRICS_READERS, self.metrics._storage)

    @measure_time(MetricsName.NODE_CHECK_PERFORMANCE_TIME)
    def checkPerformance(self) -> Optional[bool]:
        """
        Check if master instance is slow and send an instance change request.
        :returns True if master performance is OK, False if performance
        degraded, None if the check was needed
        """
        logger.trace("{} checking its performance".format(self))

        # Move ahead only if the node has synchronized its state with other
        # nodes
        if not self.isParticipating:
            return

        if self.view_change_in_progress:
            return

        if not self._update_new_ordered_reqs_count():
            logger.trace("{} ordered no new requests".format(self))
            return

        if self.instances.masterId is not None:
            self.sendNodeRequestSpike()

            master_throughput, backup_throughput = self.monitor.getThroughputs(0)
            if master_throughput is not None:
                self.metrics.add_event(MetricsName.MONITOR_AVG_THROUGHPUT, master_throughput)
            if backup_throughput is not None:
                self.metrics.add_event(MetricsName.BACKUP_MONITOR_AVG_THROUGHPUT, backup_throughput)

            avg_lat_master, avg_lat_backup = self.monitor.getLatencies()
            if avg_lat_master:
                self.metrics.add_event(MetricsName.MONITOR_AVG_LATENCY, avg_lat_master)

            if avg_lat_backup:
                self.metrics.add_event(MetricsName.BACKUP_MONITOR_AVG_LATENCY, avg_lat_backup)

            degraded_backups = self.monitor.areBackupsDegraded()
            if degraded_backups:
                logger.display('{} backup instances performance degraded'.format(degraded_backups))
                self.backup_instance_faulty_processor.on_backup_degradation(degraded_backups)

            if self.monitor.isMasterDegraded():
                logger.display('{} master instance performance degraded'.format(self))
                self.view_changer.on_master_degradation()
                return False
            else:
                logger.trace("{}'s master has higher performance than backups".
                             format(self))
        return True

    @measure_time(MetricsName.NODE_CHECK_NODE_REQUEST_SPIKE)
    def checkNodeRequestSpike(self):
        logger.trace("{} checking its request amount".format(self))

        if not self.isParticipating:
            return

        if self.instances.masterId is not None:
            self.sendNodeRequestSpike()

    def sendNodeRequestSpike(self):
        requests = self.nodeRequestSpikeMonitorData['accum']
        self.nodeRequestSpikeMonitorData['accum'] = 0
        return pluginManager.sendMessageUponSuspiciousSpike(
            notifierPluginTriggerEvents['nodeRequestSpike'],
            self.nodeRequestSpikeMonitorData,
            requests,
            self.config.notifierEventTriggeringConfig['nodeRequestSpike'],
            self.name,
            self.config.SpikeEventsEnabled
        )

    def primary_selected(self, instance_id):
        # If the node has primary replica of master instance
        if instance_id == self.master_replica.instId:
            # TODO: 0 should be replaced with configurable constant
            self.monitor.hasMasterPrimary = self.has_master_primary
            if not self.primaries_disconnection_times[self.master_replica.instId]:
                return
            if self.nodestack.isConnectedTo(self.master_primary_name) \
                    or self.master_primary_name == self.name:
                self.primaries_disconnection_times[self.master_replica.instId] = None
        else:
            primary_node_name = self.replicas[instance_id].primaryName.split(':')[0]
            if self.nodestack.isConnectedTo(primary_node_name) \
                    or primary_node_name == self.name:
                self.primaries_disconnection_times[instance_id] = None
            else:
                self.primaries_disconnection_times[instance_id] = time.perf_counter()
                self._schedule_replica_removal(instance_id)

    def propose_view_change(self):
        # Sends instance change message when primary has been
        # disconnected for long enough
        self._cancel(self.propose_view_change)
        if not self.primaries_disconnection_times[self.master_replica.instId]:
            logger.info('{} The primary is already connected '
                        'so view change will not be proposed'.format(self))
            return

        logger.display("{} primary has been disconnected for too long".format(self))

        if not self.isReady() or not self.is_synced:
            logger.info('{} The node is not ready yet '
                        'so view change will not be proposed now, but re-scheduled.'.format(self))
            self._schedule_view_change()
            return

        self.view_changer.on_primary_loss()

    def _schedule_replica_removal(self, inst_id):
        disconnection_strategy = self.config.REPLICAS_REMOVING_WITH_PRIMARY_DISCONNECTED
        if not (self.backup_instance_faulty_processor.is_local_remove_strategy(disconnection_strategy) or
                self.backup_instance_faulty_processor.is_quorum_strategy(disconnection_strategy)):
            return
        logger.info('{} scheduling replica removal for instance {} in {} sec'
                    .format(self, inst_id, self.config.TolerateBackupPrimaryDisconnection))
        self._schedule(partial(self._remove_replica_if_primary_lost, inst_id),
                       self.config.TolerateBackupPrimaryDisconnection)

    def _remove_replica_if_primary_lost(self, inst_id):
        if inst_id < len(self.primaries_disconnection_times) \
                and self.primaries_disconnection_times[inst_id] is not None \
                and time.perf_counter() - self.primaries_disconnection_times[inst_id] >= \
                self.config.TolerateBackupPrimaryDisconnection:
            self.backup_instance_faulty_processor.on_backup_primary_disconnected([inst_id])

    def _schedule_view_change(self):
        logger.info('{} scheduling a view change in {} sec'.format(self, self.config.ToleratePrimaryDisconnection))
        self._schedule(self.propose_view_change,
                       self.config.ToleratePrimaryDisconnection)

    # TODO: consider moving this to pool manager
    def lost_master_primary(self):
        """
        Schedule an primary connection check which in turn can send a view
        change message
        """
        self.primaries_disconnection_times[self.master_replica.instId] = time.perf_counter()
        self._schedule_view_change()

    def get_primaries_for_current_view(self):
        return self.primaries_selector.select_primaries(view_no=self.viewNo,
                                                        instance_count=self.requiredNumberOfInstances,
                                                        validators=self.poolManager.node_names_ordered_by_rank())

    def select_primaries(self):
        # If you want to refactor primaries selection,
        # please take a look at https://jira.hyperledger.org/browse/INDY-1946

        self.backup_instance_faulty_processor.restore_replicas()
        self.ensure_primaries_dropped()

        self.primaries = self.get_primaries_for_current_view()
        pc = len(self.primaries)
        rc = len(self.replicas)
        if pc != rc:
            raise LogicError('Inconsistent number or primaries ({}) and replicas ({})'
                             .format(pc, rc))

        for i, primary_name in enumerate(self.primaries):
            if i == 0:
                # The node needs to be set in participating mode since when
                # the replica is made aware of the primary, it will start
                # processing stashed requests and hence the node needs to be
                # participating.
                self.start_participating()

            replica = self.replicas[i]
            instance_name = Replica.generateName(nodeName=primary_name, instId=i)
            replica.primaryChanged(instance_name)
            self.primary_selected(i)

            logger.display("{}{} declares primaries selection {} as completed for "
                           "instance {}, "
                           "new primary is {}, "
                           "ledger info is {}"
                           .format(VIEW_CHANGE_PREFIX,
                                   replica,
                                   self.viewNo,
                                   i,
                                   instance_name,
                                   self.ledger_summary),
                           extra={"cli": "ANNOUNCE",
                                  "tags": ["node-election"]})

        # Notify replica, that we need to send batch with new primaries
        if self.viewNo != 0:
            self.primaries_batch_needed = True

    def _do_start_catchup(self, just_started: bool):
        # Process any already Ordered requests by the replica
        self.force_process_ordered()

        # # revert uncommitted txns and state for unordered requests
        r = self.master_replica.revert_unordered_batches()
        logger.info('{} reverted {} batches before starting catch up'.format(self, r))

        self.mode = Mode.starting
        self.ledgerManager.start_catchup(is_initial=just_started)

    def start_catchup(self, just_started=False):
        if not self.is_synced and not just_started:
            logger.info('{} does not start the catchup procedure '
                        'because another catchup is in progress'.format(self))
            return

        if self._catch_up_start_ts == 0:
            self._catch_up_start_ts = time.perf_counter()
        self._do_start_catchup(just_started)

    def ordered_prev_view_msgs(self, inst_id, pp_seqno):
        logger.debug('{} ordered previous view batch {} by instance {}'.
                     format(self, pp_seqno, inst_id))

    def verifySignature(self, msg):
        """
        Validate the signature of the request
        Note: Batch is whitelisted because the inner messages are checked

        :param msg: a message requiring signature verification
        :return: None; raises an exception if the signature is not valid
        """
        if isinstance(msg, self.authnWhitelist):
            return
        if isinstance(msg, Propagate):
            typ = 'propagate'
            req = TxnUtilConfig.client_request_class(**msg.request)
        else:
            typ = ''
            req = msg

        key = None

        if isinstance(req, Request):
            key = req.key

        if not isinstance(req, Mapping):
            req = req.as_dict

        with self.metrics.measure_time(MetricsName.VERIFY_SIGNATURE_TIME):
            identifiers = self.authNr(req).authenticate(req, key=key)

        logger.debug("{} authenticated {} signature on {} request {}".
                     format(self, identifiers, typ, req['reqId']),
                     extra={"cli": True,
                            "tags": ["node-msg-processing"]})

    def authNr(self, req):
        return self.clientAuthNr

    @measure_time(MetricsName.EXECUTE_BATCH_TIME)
    def executeBatch(self, three_pc_batch: ThreePcBatch,
                     valid_reqs_keys: List, invalid_reqs_keys: List,
                     audit_txn_root) -> None:
        """
        Execute the REQUEST sent to this Node

        :param view_no: the view number (See glossary)
        :param pp_time: the time at which PRE-PREPARE was sent
        :param valid_reqs: list of valid client requests keys
        :param valid_reqs: list of invalid client requests keys
        """

        # We need hashes in apply and str in commit
        three_pc_batch.txn_root = Ledger.hashToStr(three_pc_batch.txn_root)
        three_pc_batch.state_root = Ledger.hashToStr(three_pc_batch.state_root)

        try:
            committedTxns = self.get_executer(three_pc_batch.ledger_id)(three_pc_batch)
        except Exception as exc:
            logger.error(
                "{} commit failed for batch request, error {}, view no {}, "
                "ppSeqNo {}, ledger {}, state root {}, txn root {}, "
                "requests: {}".format(
                    self, repr(exc), three_pc_batch.view_no, three_pc_batch.pp_seq_no,
                    three_pc_batch.ledger_id, three_pc_batch.state_root,
                    three_pc_batch.txn_root, [req_idr for req_idr in valid_reqs_keys]
                )
            )
            raise

        for req_key in valid_reqs_keys + invalid_reqs_keys:
            if req_key in self.requests:
                self.mark_request_as_executed(self.requests[req_key].request)
            else:
                # Means that this request is dropped from the main requests queue due to timeout,
                # but anyway it is ordered and executed normally
                logger.debug('{} normally executed request {} which object has been dropped '
                             'from the requests queue'.format(self, req_key))
                pass

        # TODO is it possible to get len(committedTxns) != len(valid_reqs)
        # someday
        if not committedTxns:
            return

        logger.debug("{} committed batch request, view no {}, ppSeqNo {}, "
                     "ledger {}, state root {}, txn root {}, requests: {}".
                     format(self, three_pc_batch.view_no, three_pc_batch.pp_seq_no,
                            three_pc_batch.ledger_id, three_pc_batch.state_root,
                            three_pc_batch.txn_root, [key for key in valid_reqs_keys]))

        first_txn_seq_no = get_seq_no(committedTxns[0])
        last_txn_seq_no = get_seq_no(committedTxns[-1])

        reqs = []
        reqs_list_built = True
        for req_key in valid_reqs_keys:
            if req_key in self.requests:
                reqs.append(self.requests[req_key].request.as_dict)
            else:
                logger.warning("Could not build requests list for observers due to non-existent requests")
                reqs_list_built = False
                break

        if reqs_list_built:
            batch_committed_msg = BatchCommitted(reqs,
                                                 three_pc_batch.ledger_id,
                                                 0,
                                                 three_pc_batch.view_no,
                                                 three_pc_batch.pp_seq_no,
                                                 three_pc_batch.pp_time,
                                                 three_pc_batch.state_root,
                                                 three_pc_batch.txn_root,
                                                 first_txn_seq_no,
                                                 last_txn_seq_no,
                                                 audit_txn_root,
                                                 three_pc_batch.primaries)
            self._observable.append_input(batch_committed_msg, self.name)

    def updateSeqNoMap(self, committedTxns, ledger_id):
        if all([get_req_id(txn) for txn in committedTxns]):
            self.seqNoDB.addBatch((get_payload_digest(txn), ledger_id, get_seq_no(txn), get_digest(txn))
                                  for txn in committedTxns)

    def commitAndSendReplies(self, three_pc_batch: ThreePcBatch) -> List:
        logger.trace('{} going to commit and send replies to client'.format(self))
        committed_txns = self.write_manager.commit_batch(three_pc_batch)
        self.updateSeqNoMap(committed_txns, three_pc_batch.ledger_id)
        updated_committed_txns = list(map(self.update_txn_with_extra_data, committed_txns))
        self.sendRepliesToClients(updated_committed_txns, three_pc_batch.pp_time)
        return committed_txns

    def onBatchCreated(self, three_pc_batch: ThreePcBatch):
        """
        A batch of requests has been created and has been applied but
        committed to ledger and state.
        :param ledger_id:
        :param state_root: state root after the batch creation
        :return:
        """
        ledger_id = three_pc_batch.ledger_id
        if ledger_id != POOL_LEDGER_ID and not three_pc_batch.primaries:
            three_pc_batch.primaries = self.write_manager.future_primary_handler.get_last_primaries() or self.primaries
        if self.write_manager.is_valid_ledger_id(ledger_id):
            self.write_manager.post_apply_batch(three_pc_batch)
        else:
            logger.debug('{} did not know how to handle for ledger {}'.format(self, ledger_id))

    def onBatchRejected(self, ledger_id):
        """
        A batch of requests has been rejected, if stateRoot is None, reject
        the current batch.
        :param ledger_id:
        :param stateRoot: state root after the batch was created
        :return:
        """
        if self.write_manager.is_valid_ledger_id(ledger_id):
            self.write_manager.post_batch_rejected(ledger_id)
        else:
            logger.debug('{} did not know how to handle for ledger {}'.format(self, ledger_id))

    def sendRepliesToClients(self, committedTxns, ppTime):
        for txn in committedTxns:
            self.sendReplyToClient(Reply(txn),
                                   get_digest(txn))

    def sendReplyToClient(self, reply, reqKey):
        if self.isProcessingReq(reqKey):
            sender = self.requestSender[reqKey]
            if sender:
                logger.trace(
                    '{} sending reply for {} to client'.format(
                        self, reqKey))
                self.transmitToClient(reply, sender)
            else:
                logger.info('{} not sending reply for {}, since do not '
                            'know client'.format(self, reqKey))
            self.doneProcessingReq(reqKey)

    def addNewRole(self, txn):
        """
        Adds a new client or steward to this node based on transaction type.
        """
        # If the client authenticator is a simple authenticator then add verkey.
        #  For a custom authenticator, handle appropriately.
        # NOTE: The following code should not be used in production
        if isinstance(self.clientAuthNr.core_authenticator, SimpleAuthNr):
            txn_data = get_payload_data(txn)
            identifier = txn_data[TARGET_NYM]
            verkey = txn_data.get(VERKEY)
            v = DidVerifier(verkey, identifier=identifier)
            if identifier not in self.clientAuthNr.core_authenticator.clients:
                role = txn_data.get(ROLE)
                if role not in (STEWARD, TRUSTEE, None):
                    logger.debug("Role if present must be {} and not {}".
                                 format(Roles.STEWARD.name, role))
                    return
                self.clientAuthNr.core_authenticator.addIdr(identifier,
                                                            verkey=v.verkey,
                                                            role=role)

    def addGenesisNyms(self):
        # THIS SHOULD NOT BE DONE FOR PRODUCTION
        for _, txn in self.domainLedger.getAllTxn():
            if get_type(txn) == NYM:
                self.addNewRole(txn)

    def init_core_authenticator(self):
        state = self.getState(DOMAIN_LEDGER_ID)
        return CoreAuthNr(self.write_manager.txn_types,
                          self.read_manager.txn_types,
                          self.action_manager.txn_types,
                          state=state)

    def defaultAuthNr(self) -> ReqAuthenticator:
        req_authnr = ReqAuthenticator()
        req_authnr.register_authenticator(self.init_core_authenticator())
        return req_authnr

    def ensureKeysAreSetup(self):
        """
        Check whether the keys are setup in the local STP keep.
        Raises KeysNotFoundException if not found.
        """
        if not areKeysSetup(self.name, self.keys_dir):
            raise REx(REx.reason.format(self.name) + self.keygenScript)

    @staticmethod
    def reasonForClientFromException(ex: Exception):
        friendly = friendlyEx(ex)
        reason = "client request invalid: {}".format(friendly)
        return reason

    def reportSuspiciousNodeEx(self, ex: SuspiciousNode):
        """
        Report suspicion on a node on the basis of an exception
        """
        self.reportSuspiciousNode(ex.node, ex.reason, ex.code, ex.offendingMsg)

    def reportSuspiciousNode(self,
                             nodeName: str,
                             reason=None,
                             code: int = None,
                             offendingMsg=None):
        """
        Report suspicion on a node and add it to this node's blacklist.

        :param nodeName: name of the node to report suspicion on
        :param reason: the reason for suspicion
        """
        logger.warning("{} raised suspicion on node {} for {}; suspicion code "
                       "is {}".format(self, nodeName, reason, code))
        # TODO need a more general solution here

        # TODO: Should not blacklist client on a single InvalidSignature.
        # Should track if a lot of requests with incorrect signatures have been
        # made in a short amount of time, only then blacklist client.
        # if code == InvalidSignature.code:
        #     self.blacklistNode(nodeName,
        #                        reason=InvalidSignature.reason,
        #                        code=InvalidSignature.code)

        # TODO: Consider blacklisting nodes again.
        # if code in self.suspicions:
        #     self.blacklistNode(nodeName,
        #                        reason=self.suspicions[code],
        #                        code=code)

        if code in (s.code for s in (Suspicions.PPR_DIGEST_WRONG,
                                     Suspicions.PPR_REJECT_WRONG,
                                     Suspicions.PPR_TXN_WRONG,
                                     Suspicions.PPR_STATE_WRONG,
                                     Suspicions.PPR_PLUGIN_EXCEPTION,
                                     Suspicions.PPR_SUB_SEQ_NO_WRONG,
                                     Suspicions.PPR_NOT_FINAL,
                                     Suspicions.PPR_WITH_ORDERED_REQUEST,
                                     Suspicions.PPR_AUDIT_TXN_ROOT_HASH_WRONG,
                                     Suspicions.PPR_BLS_MULTISIG_WRONG,
                                     Suspicions.PPR_TIME_WRONG,
                                     )):
            logger.display('{}{} got one of primary suspicions codes {}'.format(VIEW_CHANGE_PREFIX, self, code))
            self.view_changer.on_suspicious_primary(Suspicions.get_by_code(code))

        if offendingMsg:
            self.discard(offendingMsg, reason, logger.debug)

    def reportSuspiciousClient(self, clientName: str, reason):
        """
        Report suspicion on a client and add it to this node's blacklist.

        :param clientName: name of the client to report suspicion on
        :param reason: the reason for suspicion
        """
        logger.warning("{} raised suspicion on client {} for {}"
                       .format(self, clientName, reason))
        self.blacklistClient(clientName)

    def isClientBlacklisted(self, clientName: str):
        """
        Check whether the given client is in this node's blacklist.

        :param clientName: the client to check for blacklisting
        :return: whether the client was blacklisted
        """
        return self.clientBlacklister.isBlacklisted(clientName)

    def blacklistClient(self, clientName: str,
                        reason: str = None, code: int = None):
        """
        Add the client specified by `clientName` to this node's blacklist
        """
        msg = "{} blacklisting client {}".format(self, clientName)
        if reason:
            msg += " for reason {}".format(reason)
        logger.display(msg)
        self.clientBlacklister.blacklist(clientName)

    def isNodeBlacklisted(self, nodeName: str) -> bool:
        """
        Check whether the given node is in this node's blacklist.

        :param nodeName: the node to check for blacklisting
        :return: whether the node was blacklisted
        """
        return self.nodeBlacklister.isBlacklisted(nodeName)

    def blacklistNode(self, nodeName: str, reason: str = None, code: int = None):
        """
        Add the node specified by `nodeName` to this node's blacklist
        """
        msg = "{} blacklisting node {}".format(self, nodeName)
        if reason:
            msg += " for reason {}".format(reason)
        if code:
            msg += " for code {}".format(code)
        logger.display(msg)
        self.nodeBlacklister.blacklist(nodeName)

    @property
    def blacklistedNodes(self):
        return {nm for nm in self.nodeReg.keys() if
                self.nodeBlacklister.isBlacklisted(nm)}

    def transmitToClient(self, msg: Any, remoteName: str):
        self.clientstack.transmitToClient(msg, remoteName)

    @measure_time(MetricsName.NODE_SEND_TIME)
    def send(self,
             msg: Any,
             *rids: Iterable[int],
             signer: Signer = None,
             message_splitter=None):

        if rids:
            remoteNames = [self.nodestack.remotes[rid].name for rid in rids]
            recipientsNum = len(remoteNames)
        else:
            # so it is broadcast
            remoteNames = [remote.name for remote in
                           self.nodestack.remotes.values()]
            recipientsNum = 'all'

        logger.debug("{} sending message {} to {} recipients: {}".
                     format(self, msg, recipientsNum, remoteNames))
        self.nodestack.send(msg, *rids, signer=signer, message_splitter=message_splitter)

    def sendToNodes(self, msg: Any, names: Iterable[str] = None, message_splitter=None):
        # TODO: This method exists in `Client` too, refactor to avoid
        # duplication
        rids = [rid for rid, r in self.nodestack.remotes.items(
        ) if r.name in names] if names else []
        self.send(msg, *rids, message_splitter=message_splitter)

    def getReplyFromLedgerForRequest(self, request):
        ledger_id, seq_no = self.seqNoDB.get_by_payload_digest(request.payload_digest)
        if ledger_id is not None and seq_no is not None:
            if self.seqNoDB.get_by_full_digest(request.digest) is not None:
                ledger = self.getLedger(ledger_id)
                return self.getReplyFromLedger(ledger, seq_no)
            else:
                return RequestNack(request.identifier, request.reqId,
                                   'Same txn was already ordered with different signatures or pluggable fields')
        else:
            return None

    def getReplyFromLedger(self, ledger, seq_no):
        # DoS attack vector, client requesting already processed request id
        # results in iterating over ledger (or its subset)
        txn = ledger.getBySeqNo(int(seq_no))
        if txn:
            txn.update(ledger.merkleInfo(seq_no))
            txn = self.update_txn_with_extra_data(txn)
            return Reply(txn)
        else:
            return None

    def update_txn_with_extra_data(self, txn):
        """
        All the data of the transaction might not be stored in ledger so the
        extra data that is omitted from ledger needs to be fetched from the
        appropriate data store
        :param txn:
        :return:
        """
        # All the data of any transaction is stored in the ledger
        return txn

    def transform_txn_for_ledger(self, txn):
        return self.write_manager.transform_txn_for_ledger(txn)

    def __enter__(self):
        return self

    # noinspection PyUnusedLocal
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def logstats(self):
        """
        Print the node's current statistics to log.
        """
        lines = [
            "node {} current stats".format(self),
            "--------------------------------------------------------",
            "node inbox size         : {}".format(len(self.nodeInBox)),
            "client inbox size       : {}".format(len(self.clientInBox)),
            "age (seconds)           : {}".format(time.time() - self.created),
            "next check for reconnect: {}".format(time.perf_counter() -
                                                  self.nodestack.nextCheck),
            "node connections        : {}".format(self.nodestack.conns),
            "f                       : {}".format(self.f),
            "master instance         : {}".format(self.instances.masterId),
            "replicas                : {}".format(len(self.replicas)),
            "view no                 : {}".format(self.viewNo),
            "rank                    : {}".format(self.rank),
            "msgs to replicas        : {}".format(self.replicas.sum_inbox_len),
            "msgs to view changer    : {}".format(len(self.msgsToViewChanger)),
            "action queue            : {} {}".format(len(self.actionQueue),
                                                     id(self.actionQueue)),
            "action queue stash      : {} {}".format(len(self.aqStash),
                                                     id(self.aqStash)),
        ]

        logger.info("\n".join(lines), extra={"cli": False})

    def collectNodeInfo(self):
        nodeAddress = None
        if self.poolLedger:
            for _, txn in self.poolLedger.getAllTxn():
                data = get_payload_data(txn)[DATA]
                if data[ALIAS] == self.name:
                    nodeAddress = data[NODE_IP]
                    break

        info = {
            'name': self.name,
            'rank': self.rank,
            'view': self.viewNo,
            'creationDate': self.created,
            'ledger_dir': self.ledger_dir,
            'keys_dir': self.keys_dir,
            'genesis_dir': self.genesis_dir,
            'plugins_dir': self.plugins_dir,
            'node_info_dir': self.node_info_dir,
            'portN': self.nodestack.ha[1],
            'portC': self.clientstack.ha[1],
            'address': nodeAddress
        }
        return info

    def logNodeInfo(self):
        """
        Print the node's info to log for the REST backend to read.
        """
        self.nodeInfo['data'] = self.collectNodeInfo()

        with closing(open(os.path.join(self.ledger_dir, 'node_info'), 'w')) \
                as logNodeInfoFile:
            logNodeInfoFile.write(json.dumps(self.nodeInfo['data']))

    def add_observer(self, observer_remote_id: str,
                     observer_policy_type: ObserverSyncPolicyType):
        self._observable.add_observer(
            observer_remote_id, observer_policy_type)

    def remove_observer(self, observer_remote_id):
        self._observable.remove_observer(observer_remote_id)

    def get_observers(self, observer_policy_type: ObserverSyncPolicyType):
        return self._observable.get_observers(observer_policy_type)

    def _clean_req_from_verified(self, request: Request):
        authenticator = self.authNr(request.as_dict)
        if isinstance(authenticator, ReqAuthenticator):
            authenticator.clean_from_verified(request.key)

    def mark_request_as_executed(self, request: Request):
        self.requests.mark_as_executed(request)
        self._clean_req_from_verified(request)

    def check_outdated_reqs(self):
        cur_ts = time.perf_counter()
        req_keys_to_drop = []
        for req_key in self.requests:
            outdated = False
            req_state = self.requests[req_key]

            if req_state.executed and req_state.unordered_by_replicas_num <= 0:
                # Means that the request has been processed by all replicas and
                # it just waits for stable checkpoint to be deleted.
                continue

            if req_state.added_ts is not None and \
                    cur_ts - req_state.added_ts > self.propagates_phase_req_timeout:
                outdated = True
                self.propagates_phase_req_timeouts += 1
            if req_state.finalised_ts is not None and \
                    cur_ts - req_state.finalised_ts > self.ordering_phase_req_timeout:
                outdated = True
                self.ordering_phase_req_timeouts += 1
            if outdated:
                req_keys_to_drop.append(req_key)
                self._clean_req_from_verified(req_state.request)
                self.doneProcessingReq(req_key)
                self.monitor.requestTracker.force_req_drop(req_key)
        for req_key in req_keys_to_drop:
            self.requests.force_free(req_key)

    def is_request_need_quorum(self, msg_dict: dict):
        txn_type = msg_dict.get(OPERATION).get(TXN_TYPE, None) \
            if OPERATION in msg_dict \
            else None

        return txn_type and not (txn_type == GET_TXN or
                                 self.is_action(txn_type) or
                                 self.is_query(txn_type))

    def init_req_managers(self):
        self.write_manager = WriteRequestManager(self.db_manager)
        self.read_manager = ReadRequestManager()
        self.action_manager = ActionRequestManager()

    def _bootstrap_node(self, bootstrap_cls, storage):
        bootstrap_cls(self).init_node(storage)

    def get_validators(self):
        return self.poolManager.node_ids_ordered_by_rank(
            self.nodeReg, self.poolManager.get_node_ids())

    def set_view_for_replicas(self, view_no):
        for r in self.replicas.values():
            r.set_view_no(view_no)

    def _process_start_master_catchup_msg(self, msg: NeedMasterCatchup):
        self.start_catchup()

    def _init_internal_bus(self):
        internal_bus = InternalBus()
        internal_bus.subscribe(NeedMasterCatchup, self._process_start_master_catchup_msg)
        return internal_bus

    def set_view_change_status(self, value: bool):
        """
        Remove this method after a PBFT ViewChange integration
        """
        for r in self.replicas.values():
            r.set_view_change_status(value)
