# `r n s h`  Shell over Reticulum 
[![CI](https://github.com/acehoss/rnsh/actions/workflows/python-package.yml/badge.svg)](https://github.com/acehoss/rnsh/actions/workflows/python-package.yml) 
[![Release](https://github.com/acehoss/rnsh/actions/workflows/python-publish.yml/badge.svg)](https://github.com/acehoss/rnsh/actions/workflows/python-publish.yml) 
[![PyPI version](https://badge.fury.io/py/rnsh.svg)](https://badge.fury.io/py/rnsh)  
![PyPI - Downloads](https://img.shields.io/pypi/dw/rnsh?color=informational&label=Installs&logo=pypi)

`rnsh` is a utility written in Python that facilitates shell 
sessions over [Reticulum](https://reticulum.network) networks. 
It is based on the `rnx` utility that ships with Reticulum and
aims to provide a similar experience to SSH.

## Contents

- [Alpha Disclaimer](#reminder--alpha-software)
- [Recent Changes](#recent-changes)
- [Quickstart](#quickstart)
- [Options](#options)
- [How it works](#how-it-works)
- [Roadmap](#roadmap)
- [Active TODO](#todo)

### Reminder: Alpha Software
These early versions will be buggy. There will sometimes be major
breaking changes to the command line parameters between releases.
There will sometimes be breaking changes in the protocol between
releases. Use at your own peril!

## Recent Changes
### v0.0.12
- Remove service name from RNS destination aspects. Service name
  now selects a suffix for the identity file and should only be
  supplied on the listener. The initiator only needs the destination
  hash of the listener to connect.
- Show a spinner during link establishment on tty sessions
- Attempt to catch and beautify exceptions on initiator

### v0.0.11
- Event loop bursting improves throughput and CPU utilization on
  both listener and initiator.
- Packet retries use RNS resend feature to prevent duplicate
  packets.

### v0.0.10
- Rate limit window change events to prevent saturation of transport
- Tweaked some loop timers to improve CPU utilization

### v0.0.9
- Switch to a new packet-based protocol
- Bug fixes and dependency updates

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
Note: service name no longer is supplied on initiator. The destination
      hash encapsulates this information.

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
    rnsh -l [-c <configdir>] [-i <identityfile> | -s <service_name>] [-v... | -q...] -p
    rnsh -l [-c <configdir>] [-i <identityfile> | -s <service_name>] [-v... | -q...] 
            [-b <period>] (-n | -a <identity_hash> [-a <identity_hash>] ...) [-A | -C] 
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
    -a HASH --allowed HASH       Specify identities allowed to connect
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
```

## How it works
### Listeners
Listener instances are the servers. Each listener is configured 
with an RNS identity, and a service name. Together, RNS makes
these into a destination hash that can be used to connect to
your listener.
   
Each listener must use a unique identity. The `-s` parameter
can be used to specify a service name, which creates a unique
identity file.

Listeners can be configured with a command line to run on
connect. Initiators can supply a command line as well. There 
are several different options for the way the command line 
is handled:

- `-C` no initiator command line is allowed; the connection will
  be terminated if one is supplied.
- `-A` initiator-supplied command line is appended to listener-
  configured command line
- With neither of these options, the listener will use the first 
  valid command line from this list:
  1. Initiator-supplied command line
  2. Listener command line argument
  3. Default shell of user listener is running under


If the `-n` option is not set on the listener, the initiator
is required to identify before starting a command. The program 
will be started with the initiator's identity hash string is set 
in the environment variable `RNS_REMOTE_IDENTITY`.

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
- [X] ~~Make it scriptable (currently requires a tty)~~
- [X] ~~Protocol improvements (throughput!)~~
- [X] ~~Documentation improvements~~
- [ ] Test on several platforms
- [ ] Fix issues that come up with testing
- [ ] Fix issues with running `rnsh` in a binary pipeline, i.e. 
  piping the output of `tar` over `rsh`.
- [ ] Beta release
- [ ] Test and fix more issues
- [ ] V1.0
- [ ] Enhancement Ideas
  - [ ] `authorized_keys` mode similar to SSH
  - [ ] Git over `rnsh` (git remote helper)
  - [ ] Sliding window acknowledgements for improved throughput

## Miscellaneous

By piping into/out of `rnsh`, it should be possible to transfer
files using the same method discussed in 
[this article](https://cromwell-intl.com/open-source/tar-and-ssh.html).
I tested it just now and it doesn't work right. There's probably some
subtle garbling of the data at one end of the stream or the other.
