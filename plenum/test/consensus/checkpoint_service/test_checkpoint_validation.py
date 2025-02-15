import pytest

from plenum.common.messages.node_messages import Checkpoint
from plenum.common.startable import Mode
from plenum.common.util import SortedDict
from plenum.server.consensus.msg_validator import CheckpointMsgValidator
from plenum.server.replica_validator import ReplicaValidator
from plenum.server.replica_validator_enums import DISCARD, INCORRECT_INSTANCE, PROCESS, CATCHING_UP, ALREADY_STABLE, \
    STASH_CATCH_UP, OLD_VIEW, FUTURE_VIEW, STASH_VIEW


@pytest.fixture(scope='function', params=[0, 1])
def inst_id(request):
    return request.param


@pytest.fixture(scope='function', params=[2])
def viewNo(tconf, request):
    return request.param


@pytest.fixture(scope='function')
def validator(consensus_data):
    data = consensus_data("some_name")
    data.node_mode = Mode.participating
    return CheckpointMsgValidator(data)


def checkpoint(view_no, inst_id, seq_no_start, seq_no_end):
    return Checkpoint(instId=inst_id,
                      viewNo=view_no,
                      seqNoStart=seq_no_start,
                      seqNoEnd=seq_no_end,
                      digest='digest-{}-{}'.format(str(seq_no_start), str(seq_no_end)))


def test_check_all_correct(validator):
    msg = checkpoint(view_no=validator._data.view_no,
                     inst_id=validator._data.inst_id,
                     seq_no_start=0,
                     seq_no_end=10)
    assert validator.validate(msg) == (PROCESS, None)


def test_check_inst_id_incorrect(validator):
    msg = checkpoint(view_no=validator._data.view_no,
                     inst_id=validator._data.inst_id + 1,
                     seq_no_start=0,
                     seq_no_end=10)
    assert validator.validate(msg) == (DISCARD, INCORRECT_INSTANCE)


@pytest.mark.parametrize('mode, result', [
    (Mode.starting, (STASH_CATCH_UP, CATCHING_UP)),
    (Mode.discovering, (STASH_CATCH_UP, CATCHING_UP)),
    (Mode.discovered, (STASH_CATCH_UP, CATCHING_UP)),
    (Mode.syncing, (STASH_CATCH_UP, CATCHING_UP)),
    (Mode.synced, (STASH_CATCH_UP, CATCHING_UP)),
    (Mode.participating, (PROCESS, None)),
])


def test_check_participating(validator, mode, result):
    validator._data.node_mode = mode
    msg = checkpoint(view_no=validator._data.view_no,
                     inst_id=validator._data.inst_id,
                     seq_no_start=0,
                     seq_no_end=10)
    assert validator.validate(msg) == result


@pytest.mark.parametrize('seq_no_end, result', [
    (0, (DISCARD, ALREADY_STABLE)),
    (1, (DISCARD, ALREADY_STABLE)),
    (19, (DISCARD, ALREADY_STABLE)),
    (20, (DISCARD, ALREADY_STABLE)),
    # assume stable is 10
    (21, (PROCESS, None)),
    (22, (PROCESS, None)),
    (100, (PROCESS, None)),
])
def test_check_stable(validator, seq_no_end, result):
    validator._is_pp_seq_no_stable = lambda msg: msg.seqNoEnd <= 20
    msg = checkpoint(view_no=validator._data.view_no,
                     inst_id=validator._data.inst_id,
                     seq_no_start=0,
                     seq_no_end=seq_no_end)
    assert validator.validate(msg) == result


@pytest.mark.parametrize('seq_no_end, result', [
    (0, (DISCARD, ALREADY_STABLE)),
    (1, (DISCARD, ALREADY_STABLE)),
    (19, (DISCARD, ALREADY_STABLE)),
    (20, (DISCARD, ALREADY_STABLE)),
    # assume stable is 10
    (21, (STASH_CATCH_UP, CATCHING_UP)),
    (22, (STASH_CATCH_UP, CATCHING_UP)),
    (100, (STASH_CATCH_UP, CATCHING_UP)),
])
def test_check_stable_not_participating(validator, seq_no_end, result):
    validator._is_pp_seq_no_stable = lambda msg: msg.seqNoEnd <= 20
    validator._data.node_mode = Mode.syncing
    msg = checkpoint(view_no=validator._data.view_no,
                     inst_id=validator._data.inst_id,
                     seq_no_start=0,
                     seq_no_end=seq_no_end)
    assert validator.validate(msg) == result


def test_check_old_view(validator):
    msg = checkpoint(view_no=validator._data.view_no,
                     inst_id=validator._data.inst_id,
                     seq_no_start=0,
                     seq_no_end=10)
    validator._data.view_no += 1
    assert validator.validate(msg) == (DISCARD, OLD_VIEW)


def test_check_future_view(validator):
    msg = checkpoint(view_no=validator._data.view_no + 1,
                     inst_id=validator._data.inst_id,
                     seq_no_start=0,
                     seq_no_end=10)
    assert validator.validate(msg) == (STASH_VIEW, FUTURE_VIEW)


def test_check_view_chnange(validator):
    validator._data.legacy_vc_in_progress = True
    msg = checkpoint(view_no=validator._data.view_no,
                     inst_id=validator._data.inst_id,
                     seq_no_start=0,
                     seq_no_end=10)
    assert validator.validate(msg) == (STASH_VIEW, FUTURE_VIEW)
