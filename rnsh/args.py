from typing import TypeVar
import RNS
import importlib.metadata
import docopt
import sys

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
    rnsh [--config <configdir>] [-i <identityfile>] [-s <service_name>] [-l] -p
    rnsh -l [--config <configfile>] [-i <identityfile>] [-s <service_name>] 
         [-v... | -q...] [-b <period>] (-n | -a <identity_hash> [-a <identity_hash>] ...) 
         [-C] [[--] <program> [<arg> ...]]
    rnsh [--config <configfile>] [-i <identityfile>] [-s <service_name>] 
         [-v... | -q...] [-N] [-m] [-w <timeout>] <destination_hash> 
         [[--] <program> [<arg> ...]]
    rnsh -h
    rnsh --version

Options:
    --config DIR             Alternate Reticulum config directory to use
    -i FILE --identity FILE  Specific identity file to use
    -s NAME --service NAME   Listen on/connect to specific service name if not default
    -p --print-identity      Print identity information and exit
    -l --listen              Listen (server) mode. If supplied, <program> <arg>...will 
                               be used as the command line when the initiator does not
                               provide one or when remote command is disabled. If
                               <program> is not supplied, the default shell of the 
                               user rnsh is running under will be used.
    -b --announce PERIOD     Announce on startup and every PERIOD seconds
                             Specify 0 for PERIOD to announce on startup only.
    -a HASH --allowed HASH   Specify identities allowed to connect
    -n --no-auth             Disable authentication
    -N --no-id               Disable identify on connect
    -C --no-remote-command   Disable executing command line from remote
    -m --mirror              Client returns with code of remote process
    -w TIME --timeout TIME   Specify client connect and request timeout in seconds
    -q --quiet               Increase quietness (move level up), multiple increases effect
                                     DEFAULT LOGGING LEVEL
                                              CRITICAL (silent)
                                Initiator ->  ERROR
                                              WARNING
                                 Listener ->  INFO
                                              DEBUG    (insane)
    -v --verbose             Increase verbosity (move level down), multiple increases effect
    --version                Show version
    -h --help                Show this help
'''


class Args:
    def __init__(self, argv: [str]):
        global usage
        try:
            argv, program_args = _split_array_at(argv, "--")
            if len(program_args) > 0:
                argv.append(program_args[0])
                self.program_args = program_args[1:]
    
            args = docopt.docopt(usage, argv=argv[1:], version=f"rnsh {importlib.metadata.version('rnsh')}")
            # json.dump(args, sys.stdout)
    
            self.service_name = args.get("--service", None) or "default"
            self.listen = args.get("--listen", None) or False
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
                self.valid = False
            self.no_auth = args.get("--no-auth", None) or False
            self.allowed = args.get("--allowed", None) or []
            self.no_remote_cmd = args.get("--no-remote-command", None) or False
            self.program = args.get("<program>", None)
            self.program_args = args.get("<arg>", None) or []
            if self.program is not None:
                self.program_args.insert(0, self.program)
            self.program_args.extend(program_args)
            self.no_id = args.get("--no-id", None) or False
            self.mirror = args.get("--mirror", None) or False
            self.timeout = args.get("--timeout", None) or RNS.Transport.PATH_REQUEST_TIMEOUT
            self.destination = args.get("<destination_hash>", None)
            self.help = args.get("--help", None) or False
        except Exception as e:
            print("Error parsing arguments: {e}")
            print()
            print(usage)
            sys.exit(1)

        if self.help:
            sys.exit(0)
