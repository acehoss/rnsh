from typing import TypeVar
import RNS
import rnsh
import sys
from rnsh import docopt

_T = TypeVar("_T")


def _split_array_at(arr: [_T], at: _T) -> ([_T], [_T]):
    try:
        idx = arr.index(at)
        return arr[:idx], arr[idx + 1:]
    except ValueError:
        return arr, []


usage = \
'''
Usage:
    rnsh -l [-c <configdir>] [-i <identityfile> | -s <service_name>] [-v... | -q...] -p
    rnsh -l [-c <configdir>] [-i <identityfile> | -s <service_name>] [-v... | -q...] 
            [-b <period>] [-n] [-a <identity_hash>] ([-a <identity_hash>] ...) [-A | -C]
            [[--] <program> [<arg> ...]]
    rnsh [-c <configdir>] [-i <identityfile>] [-v... | -q...] -p
    rnsh [-c <configdir>] [-i <identityfile>] [-v... | -q...] [-N] [-m] [-w <timeout>] 
         <destination_hash> [[--] <program> [<arg> ...]]
    rnsh -h
    rnsh --version

Options:
    -c DIR --config DIR          Alternate Reticulum config directory to use
    -i FILE --identity FILE      Specific identity file to use
    -s NAME --service NAME       Service name for identity file if not default
    -p --print-identity          Print identity information and exit
    -l --listen                  Listen (server) mode. If supplied, <program> <arg>...will 
                                   be used as the command line when the initiator does not
                                   provide one or when remote command is disabled. If
                                   <program> is not supplied, the default shell of the 
                                   user rnsh is running under will be used.
    -b --announce PERIOD         Announce on startup and every PERIOD seconds
                                 Specify 0 for PERIOD to announce on startup only.
    -a HASH --allowed HASH       Specify identities allowed to connect. Allowed identities
                                   can also be specified in ~/.rnsh/allowed_identities or
                                   ~/.config/rnsh/allowed_identities, one hash per line.
    -n --no-auth                 Disable authentication
    -N --no-id                   Disable identify on connect
    -A --remote-command-as-args  Concatenate remote command to argument list of <program>/shell
    -C --no-remote-command       Disable executing command line from remote
    -m --mirror                  Client returns with code of remote process
    -w TIME --timeout TIME       Specify client connect and request timeout in seconds
    -q --quiet                   Increase quietness (move level up), multiple increases effect
                                          DEFAULT LOGGING LEVEL
                                                  CRITICAL (silent)
                                    Initiator ->  ERROR
                                                  WARNING
                                     Listener ->  INFO
                                                  DEBUG    (insane)
    -v --verbose                 Increase verbosity (move level down), multiple increases effect
    --version                    Show version
    -h --help                    Show this help
'''

DEFAULT_SERVICE_NAME = "default"

class Args:
    def __init__(self, argv: [str]):
        global usage
        try:
            self.argv = argv
            self.program_args = []
            self.docopts_argv, self.program_args = _split_array_at(self.argv, "--")
            # need to add first arg after -- back onto argv for docopts, but only for listener
            if next(filter(lambda a: a == "-l" or a == "--listen", self.docopts_argv), None) is not None \
                    and len(self.program_args) > 0:
                self.docopts_argv.append(self.program_args[0])
                self.program_args = self.program_args[1:]
    
            args = docopt.docopt(usage, argv=self.docopts_argv[1:], version=f"rnsh {rnsh.__version__}")
            # json.dump(args, sys.stdout)

            self.listen = args.get("--listen", None) or False
            self.service_name = args.get("--service", None)
            # Default service name only when listening and not explicitly provided
            if self.listen and (self.service_name is None or len(self.service_name) == 0):
                self.service_name = DEFAULT_SERVICE_NAME
            self.identity = args.get("--identity", None)
            self.config = args.get("--config", None)
            self.print_identity = args.get("--print-identity", None) or False
            self.verbose = args.get("--verbose", None) or 0
            self.quiet = args.get("--quiet", None) or 0
            announce = args.get("--announce", None)
            self.announce = None
            try:
                if announce:
                    self.announce = int(announce)
            except ValueError:
                print("Invalid value for --announce")
                sys.exit(1)
            self.no_auth = args.get("--no-auth", None) or False
            self.allowed = args.get("--allowed", None) or []
            self.remote_cmd_as_args = args.get("--remote-command-as-args", None) or False
            self.no_remote_cmd = args.get("--no-remote-command", None) or False
            self.program = args.get("<program>", None)
            if len(self.program_args) == 0:
                self.program_args = args.get("<arg>", None) or []
            self.no_id = args.get("--no-id", None) or False
            self.mirror = args.get("--mirror", None) or False
            timeout = args.get("--timeout", None)
            self.timeout = None
            try:
                if timeout:
                    self.timeout = int(timeout)
            except ValueError:
                print("Invalid value for --timeout")
                sys.exit(1)
            self.destination = args.get("<destination_hash>", None)
            self.help = args.get("--help", None) or False
            self.command_line = [self.program] if self.program else []
            self.command_line.extend(self.program_args)
        except docopt.DocoptExit:
            print()
            print(usage)
            sys.exit(1)
        except Exception as e:
            print(f"Error parsing arguments: {e}")
            print()
            print(usage)
            sys.exit(1)

        if self.help:
            sys.exit(0)
