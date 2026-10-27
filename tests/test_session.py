"""ListenerSession state machine, driven directly with fake transport.

Regression coverage for the identity gate: a rejected identity must not be able to
get a command executed in the window between terminate() and the deferred prune.
"""
import pytest

import rnsh.process
import rnsh.protocol as protocol
import rnsh.session as session
from rnsh.session import LSState, ListenerSession


class FakeLoop:
    """Runs call_soon_threadsafe callbacks inline and records call_later without firing,
    which is exactly the window an attacker has after terminate() schedules the prune."""
    def __init__(self):
        self.later = []

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)

    def call_later(self, delay, fn, *args):
        self.later.append((delay, fn, args))


class FakeOutlet:
    def __init__(self):
        self.identified_cb = None
        self.closed_cb = None
        self.torn_down = False

    def set_initiator_identified_callback(self, cb):
        self.identified_cb = cb

    def set_link_closed_callback(self, cb):
        self.closed_cb = cb

    def unset_link_closed_callback(self):
        self.closed_cb = None

    @property
    def rtt(self):
        return 0.1

    def teardown(self):
        self.torn_down = True


class FakeChannel:
    mdu = 400

    def __init__(self):
        self.sent = []
        self.handler = None

    def register_message_type(self, *args, **kwargs):
        pass

    def add_message_handler(self, handler):
        self.handler = handler

    def send(self, message):
        self.sent.append(message)
        return message

    def is_ready_to_send(self):
        return True


class FakeIdentity:
    def __init__(self, hash_: bytes):
        self.hash = hash_

    def __str__(self):
        return self.hash.hex()


class ProcessSpy:
    """Stands in for CallbackSubprocess; records whether anything tried to start a command."""
    started = []
    PROCESS_PIPE_TIME = rnsh.process.CallbackSubprocess.PROCESS_PIPE_TIME

    def __init__(self, *args, **kwargs):
        ProcessSpy.started.append(kwargs.get("argv"))
        self.running = False

    def start(self):
        pass

    def set_winsize(self, *args):
        pass

    def terminate(self, *args, **kwargs):
        pass


ALLOWED = FakeIdentity(b"\x01" * 16)
STRANGER = FakeIdentity(b"\x02" * 16)


@pytest.fixture
def listener(monkeypatch):
    monkeypatch.setattr(ListenerSession, "sessions", [])
    monkeypatch.setattr(ListenerSession, "allow_all", False)
    monkeypatch.setattr(ListenerSession, "allowed_identity_hashes", [ALLOWED.hash])
    monkeypatch.setattr(ListenerSession, "allowed_file_identity_hashes", [])
    monkeypatch.setattr(ListenerSession, "allow_remote_command", True)
    monkeypatch.setattr(ListenerSession, "default_command", [])
    monkeypatch.setattr(session.process, "CallbackSubprocess", ProcessSpy)
    ProcessSpy.started = []
    outlet, channel, loop = FakeOutlet(), FakeChannel(), FakeLoop()
    sess = ListenerSession(outlet, channel, loop)
    return sess, outlet, channel, loop


def _execute(cmdline):
    return protocol.ExecuteCommandMesssage(cmdline=cmdline, pipe_stdin=True, pipe_stdout=True, pipe_stderr=True,
                                           tcflags=[], term="xterm", rows=24, cols=80, hpix=0, vpix=0)


def test_allowed_identity_can_run_a_command(listener):
    sess, outlet, channel, loop = listener
    assert sess.state == LSState.LSSTATE_WAIT_IDENT
    assert not sess.authenticated

    outlet.identified_cb(outlet, ALLOWED)
    assert sess.authenticated
    assert sess.state == LSState.LSSTATE_WAIT_VERS

    channel.handler(protocol.VersionInfoMessage())
    assert sess.state == LSState.LSSTATE_WAIT_CMD
    assert any(isinstance(m, protocol.VersionInfoMessage) for m in channel.sent)

    channel.handler(_execute(["/bin/true"]))
    assert sess.state == LSState.LSSTATE_RUNNING
    assert ProcessSpy.started == [["/bin/true"]]


def test_rejected_identity_cannot_run_a_command(listener):
    sess, outlet, channel, loop = listener

    outlet.identified_cb(outlet, STRANGER)

    # Rejected: error sent, session dead, teardown scheduled but not yet run.
    assert not sess.authenticated
    assert sess.terminated
    assert sess.state == LSState.LSSTATE_ERROR
    assert sess.remote_identity is None
    assert any(isinstance(m, protocol.ErrorMessage) for m in channel.sent)
    assert any(fn == sess._prune for _, fn, _ in loop.later)

    # A client that ignores the error and keeps talking gets nowhere.
    channel.handler(protocol.VersionInfoMessage())
    channel.handler(_execute(["/bin/true"]))
    assert sess.state == LSState.LSSTATE_ERROR
    assert ProcessSpy.started == []


def test_unauthenticated_message_before_identification_is_ignored(listener):
    sess, outlet, channel, loop = listener

    channel.handler(protocol.VersionInfoMessage())
    channel.handler(_execute(["/bin/true"]))
    assert sess.state == LSState.LSSTATE_WAIT_IDENT
    assert ProcessSpy.started == []


def test_start_cmd_refuses_without_authentication(listener):
    sess, outlet, channel, loop = listener
    sess.state = LSState.LSSTATE_RUNNING  # even if state were forced, auth still gates it

    sess._start_cmd(["/bin/true"], True, True, True, [], "xterm", 24, 80, 0, 0)
    assert ProcessSpy.started == []
    assert sess.terminated


def test_identify_twice_after_rejection_does_not_revive(listener):
    sess, outlet, channel, loop = listener
    outlet.identified_cb(outlet, STRANGER)
    outlet.identified_cb(outlet, ALLOWED)
    assert not sess.authenticated
    assert sess.state == LSState.LSSTATE_ERROR


def test_exploit_sequence_never_starts_a_process(listener):
    # The bypass as an attacker would drive it, with no assertions on the new
    # bookkeeping: on the pre-fix code this fails because the command runs.
    sess, outlet, channel, loop = listener
    outlet.identified_cb(outlet, STRANGER)
    channel.handler(protocol.VersionInfoMessage())
    channel.handler(_execute(["/bin/true"]))
    assert ProcessSpy.started == []
