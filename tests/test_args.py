import rnsh.args
import shlex
import docopt

def test_program_args():
    docopt_threw = False
    try:
        args = rnsh.args.Args(shlex.split("rnsh -l -n one two three"))
        assert args.listen
        assert args.program == "one"
        assert args.program_args == ["two", "three"]
        assert args.command_line == ["one", "two", "three"]
    except docopt.DocoptExit:
        docopt_threw = True
    assert not docopt_threw


def test_program_args_dash():
    docopt_threw = False
    try:
        args = rnsh.args.Args(shlex.split("rnsh -l -n -- one -l -C"))
        assert args.listen
        assert args.program == "one"
        assert args.program_args == ["-l", "-C"]
        assert args.command_line == ["one", "-l", "-C"]
    except docopt.DocoptExit:
        docopt_threw = True
    assert not docopt_threw

def test_program_initiate_no_args():
    docopt_threw = False
    try:
        args = rnsh.args.Args(shlex.split("rnsh one"))
        assert not args.listen
        assert args.destination == "one"
        assert args.command_line == []
    except docopt.DocoptExit:
        docopt_threw = True
    assert not docopt_threw

def test_program_initiate_dash_args():
    docopt_threw = False
    try:
        args = rnsh.args.Args(shlex.split("rnsh --config ~/Projects/rnsh/testconfig -vvvvvvv a5f72aefc2cb3cdba648f73f77c4e887 -- -l"))
        assert not args.listen
        assert args.config == "~/Projects/rnsh/testconfig"
        assert args.verbose == 7
        assert args.destination == "a5f72aefc2cb3cdba648f73f77c4e887"
        assert args.command_line == ["-l"]
    except docopt.DocoptExit:
        docopt_threw = True
    assert not docopt_threw


def test_program_listen_dash_args():
    docopt_threw = False
    try:
        args = rnsh.args.Args(shlex.split("rnsh -l --config ~/Projects/rnsh/testconfig -n -C -- /bin/pwd"))
        assert args.listen
        assert args.config == "~/Projects/rnsh/testconfig"
        assert args.destination is None
        assert args.no_auth
        assert args.no_remote_cmd
        assert args.command_line == ["/bin/pwd"]
    except docopt.DocoptExit:
        docopt_threw = True
    assert not docopt_threw


def test_program_listen_config_print():
    docopt_threw = False
    try:
        args = rnsh.args.Args(shlex.split("rnsh -l --config testconfig -p"))
        assert args.listen
        assert args.config == "testconfig"
        assert args.print_identity
        assert args.command_line == []
    except docopt.DocoptExit:
        docopt_threw = True
    assert not docopt_threw


def test_split_at():
    a, b = rnsh.args._split_array_at(["one", "two", "three"], "two")
    assert a == ["one"]
    assert b == ["three"]

def test_split_at_not_found():
    a, b = rnsh.args._split_array_at(["one", "two", "three"], "four")
    assert a == ["one", "two", "three"]
    assert b == []