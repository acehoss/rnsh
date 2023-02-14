# `r n s h`  Shell over Reticulum 
[![CI](https://github.com/acehoss/rnsh/actions/workflows/python-package.yml/badge.svg)](https://github.com/acehoss/rnsh/actions/workflows/python-package.yml) 
[![Release](https://github.com/acehoss/rnsh/actions/workflows/python-publish.yml/badge.svg)](https://github.com/acehoss/rnsh/actions/workflows/python-publish.yml) 
[![PyPI version](https://badge.fury.io/py/rnsh.svg)](https://badge.fury.io/py/rnsh)  
![PyPI - Downloads](https://img.shields.io/pypi/dw/rnsh?color=informational&label=Installs&logo=pypi)

`rnsh` is a utility written in Python that facilitates shell 
sessions over [Reticulum](https://reticulum.network) networks. 
It is based on the `rnx` utility that ships with Reticulum and
aims to provide a similar experience to SSH.

`rnsh` is still a little raw; there are some things that are 
implemented badly, and many other things that haven't been 
built at all (yet). Signals (i.e. Ctrl-C) need some work, so have
another terminal handy to send a SIGTERM if things glitch
out.

Anyway, there's a lot of room for improvement.

## New in v0.0.5
### Remote command line and pipe compatibility
Command line options have changed somewhat to allow the initiator
to supply a command line. This allows `rnsh` to function similarly
to SSH. You can pipe into or out of `rnsh` to send input through
remote commands or remote command output through other commands.

This behavior can be blocked on the listener with the `-C` option.

When the initiator does not supply a command, the listener uses
a default command specified on its command line. If a default
command is not specified, the listener falls back to the shell
of the user it is running under.

## Quickstart

Tested (thus far) on Python 3.11 macOS 13.1 ARM64. Should
run on Python 3.6+ on Linux or Unix. WSL probably works. 
Cygwin might work, too.

- Activate a virtualenv
- `pip3 install rnsh`
  - Or from a `whl` release, `pip3 install /path/to/rnsh-0.0.1-py3-none-any.whl`
- Configure Reticulum interfaces, check with `rnstatus`
- Ready to run `rnsh`. The options are shown below.

### Example: Shell server
#### Setup
Before running the listener or initiator, you'll need to get the 
listener destination hash and the initiator identity hash.
```shell
# On listener
rnsh -l -p

# On initiator
rnsh -p
```
Note: if you are using a non-default identity or service name, be
sure to supply these options with `-p` as the identity and 
destination hashes will change depending on these settings.

#### Listener
- Listening for default service name ("default").
- Using user's default Reticulum config dir (~/.reticulum).
- Using default identity ($RNSCONFIGDIR/storage/identities/rnsh).
- Allowing remote identity `6d47805065fa470852cf1b1ef417a1ac` to connect.
- Launching `/bin/zsh` on authorized connect.
```shell
rnsh -l -a 6d47805065fa470852cf1b1ef417a1ac -- /bin/zsh
```
#### Initiator
- Connecting to default service name ("default").
- Using user's default Reticulum config dir (~/.reticulum).
- Using default identity ($RNSCONFIGDIR/storage/identities/rnsh).
- Connecting to destination `a5f72aefc2cb3cdba648f73f77c4e887`
```shell
rnsh a5f72aefc2cb3cdba648f73f77c4e887
```

## Options
```
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
```

## How it works
### Listeners
Listener instances are the servers. Each listener is configured 
with an RNS identity, and a service name. Together, RNS makes
these into a destination hash that can be used to connect to
your listener.
   
Multiple listeners can use the same identity. As long as 
they are given different service names. They will have 
different destination hashes and not conflict.

Listeners must be configured with a command line to run (at 
least at this time). The identity hash string is set in the
environment variable RNS_REMOTE_IDENTITY for use in child
programs.

Listeners are set up using the `-l` flag.
   
### Initiators
Initiators are the clients. Each initiator has an identity
hash which is used as an authentication mechanism on Reticulum.
You'll need this value to configure the listener to allow 
your connection. It is possible to run the server without 
authentication, but hopefully it's obvious that this is an
advanced use case. 
    
To get the identity hash, use the `-p` flag.
    
With the initiator identity set up in the listener command
line, and with the listener identity copied (you'll need to
do `-p` on the listener side, too), you can run the
initiator.
    
I recommend staying pretty vanilla to start with and
trying `/bin/zsh` or whatever your favorite shell is these 
days. The shell should start in login mode. Ideally it
works just like an `ssh` shell session.

### Protocol
The protocol is build on top of the Reticulum `Request` and
`Packet` APIs.

- After the initiator identifies on the connection, it enters
  a request loop. 
- When idle, the initiator will periodically 
  poll the listener. 
- When the initiator has data available (i.e the user typed 
  some characters), the initiator will send that data to the
  listener in a request, and the listener will respond with 
  any data available from the listener. 
- When the listener has new data available, it notifies the 
  initiator using a notification packet. The initiator then 
  makes a request to the listener to fetch the data.
   
## Roadmap
1. Plan a better roadmap
2. ?
3. Keep my day job

## TODO
- [X] ~~Initial version~~
- [X] ~~Pip package with command-line utility support~~
- [X] ~~Publish to PyPI~~
- [X] ~~Improve signal handling~~
- [ ] Protocol improvements (throughput!)
- [ ] Test on several *nixes
- [X] ~~Make it scriptable (currently requires a tty)~~
- [ ] Documentation improvements
