import pytest
from plenum.common.constants import DOMAIN_LEDGER_ID, CURRENT_PROTOCOL_VERSION, AUDIT_LEDGER_ID, POOL_LEDGER_ID, \
    SEQ_NO_DB_LABEL
from plenum.common.exceptions import SuspiciousNode
from plenum.common.messages.internal_messages import RequestPropagates
from plenum.common.messages.node_messages import PrePrepare
from plenum.server.replica import PP_SUB_SEQ_NO_WRONG, PP_NOT_FINAL
from plenum.server.suspicion_codes import Suspicions
from plenum.test.consensus.order_service.helper import _register_pp_ts
from plenum.test.helper import sdk_random_request_objects, create_pre_prepare_params
from plenum.test.testing_utils import FakeSomething


@pytest.fixture(scope="function")
def pre_prepare(orderer, _pre_prepare):
    _register_pp_ts(orderer, _pre_prepare, orderer.primary_name)
    return _pre_prepare


@pytest.fixture()
def fake_requests():
    return sdk_random_request_objects(10, identifier="fake_did",
                                      protocol_version=CURRENT_PROTOCOL_VERSION)


@pytest.fixture(scope='function')
def orderer_with_requests(orderer, fake_requests):
    orderer.l_apply_pre_prepare = lambda a: (fake_requests, [], [], False)
    for req in fake_requests:
        orderer.requestQueues[DOMAIN_LEDGER_ID].add(req.key)
        orderer._requests.add(req)
        orderer._requests.set_finalised(req)

    return orderer


def expect_suspicious(orderer, suspicious_code):
    def reportSuspiciousNodeEx(ex):
        assert suspicious_code == ex.code
        raise ex

    orderer.report_suspicious_node = reportSuspiciousNodeEx


def test_process_pre_prepare_validation(orderer_with_requests,
                                        pre_prepare):
    orderer_with_requests.process_preprepare(pre_prepare, orderer_with_requests.primary_name)


def test_process_pre_prepare_with_incorrect_pool_state_root(orderer_with_requests,
                                                            state_roots, txn_roots, multi_sig, fake_requests):
    expect_suspicious(orderer_with_requests, Suspicions.PPR_POOL_STATE_ROOT_HASH_WRONG.code)

    pre_prepare_params = create_pre_prepare_params(state_root=state_roots[DOMAIN_LEDGER_ID],
                                                   ledger_id=DOMAIN_LEDGER_ID,
                                                   txn_root=txn_roots[DOMAIN_LEDGER_ID],
                                                   bls_multi_sig=multi_sig,
                                                   view_no=orderer_with_requests.view_no,
                                                   inst_id=0,
                                                   # INVALID!
                                                   pool_state_root="HSai3sMHKeAva4gWMabDrm1yNhezvPHfXnGyHf2ex1L4",
                                                   audit_txn_root=txn_roots[AUDIT_LEDGER_ID],
                                                   reqs=fake_requests,
                                                   pp_seq_no=1)
    pre_prepare = PrePrepare(*pre_prepare_params)
    _register_pp_ts(orderer_with_requests, pre_prepare, orderer_with_requests.primary_name)

    with pytest.raises(SuspiciousNode):
        orderer_with_requests.process_preprepare(pre_prepare, orderer_with_requests.primary_name)


def test_process_pre_prepare_with_incorrect_audit_txn_root(orderer_with_requests,
                                                           state_roots, txn_roots, multi_sig, fake_requests):
    expect_suspicious(orderer_with_requests, Suspicions.PPR_AUDIT_TXN_ROOT_HASH_WRONG.code)

    pre_prepare_params = create_pre_prepare_params(state_root=state_roots[DOMAIN_LEDGER_ID],
                                                   ledger_id=DOMAIN_LEDGER_ID,
                                                   txn_root=txn_roots[DOMAIN_LEDGER_ID],
                                                   bls_multi_sig=multi_sig,
                                                   view_no=orderer_with_requests.view_no,
                                                   inst_id=0,
                                                   pool_state_root=state_roots[POOL_LEDGER_ID],
                                                   # INVALID!
                                                   audit_txn_root="HSai3sMHKeAva4gWMabDrm1yNhezvPHfXnGyHf2ex1L4",
                                                   reqs=fake_requests,
                                                   pp_seq_no=1)
    pre_prepare = PrePrepare(*pre_prepare_params)
    _register_pp_ts(orderer_with_requests, pre_prepare, orderer_with_requests.primary_name)

    with pytest.raises(SuspiciousNode):
        orderer_with_requests.process_preprepare(pre_prepare, orderer_with_requests.primary_name)


def test_process_pre_prepare_with_not_final_request(orderer, pre_prepare):
    orderer.db_manager.stores[SEQ_NO_DB_LABEL] = FakeSomething(get_by_full_digest=lambda req: None,
                                                               get_by_payload_digest=lambda req: (None, None))
    orderer.l_nonFinalisedReqs = lambda a: set(pre_prepare.reqIdr)

    def request_propagates(reqs):
        assert reqs == set(pre_prepare.reqIdr)

    orderer._bus.subscribe(RequestPropagates, request_propagates)

    orderer.process_preprepare(pre_prepare, orderer.primary_name)


def test_process_pre_prepare_with_ordered_request(orderer, pre_prepare):
    expect_suspicious(orderer, Suspicions.PPR_WITH_ORDERED_REQUEST.code)

    orderer.db_manager.stores[SEQ_NO_DB_LABEL] = FakeSomething(get_by_full_digest=lambda req: 'sample',
                                                               get_by_payload_digest=lambda req: (1, 1))
    orderer.l_nonFinalisedReqs = lambda a: pre_prepare.reqIdr

    def request_propagates(reqs):
        assert False, "Requested propagates for: {}".format(reqs)

    orderer._bus.subscribe(RequestPropagates, request_propagates)

    with pytest.raises(SuspiciousNode):
        orderer.process_preprepare(pre_prepare, orderer.primary_name)


def test_suspicious_on_wrong_sub_seq_no(orderer_with_requests, pre_prepare):
    pre_prepare.sub_seq_no = 1
    assert PP_SUB_SEQ_NO_WRONG == orderer_with_requests.l_process_valid_preprepare(pre_prepare,
                                                                                   orderer_with_requests.primary_name)


def test_suspicious_on_not_final(orderer_with_requests, pre_prepare):
    pre_prepare.final = False
    assert PP_NOT_FINAL == orderer_with_requests.l_process_valid_preprepare(pre_prepare,
                                                                            orderer_with_requests.primary_name)