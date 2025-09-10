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

### Reminder: Beta Software
The interface is starting to firm up, but some bug fixes at this
point still may introduce breaking changes, especially in the
protocol layers of the software.

## Recent Changes
### v0.1.6
- Fix listener `-s/--service` parsing so provided service names are honored. This ensures
  different service names use distinct identity files and destination hashes.

### v0.1.4
- Fix invalid escape sequence handling for terminal escape sequences

### v0.1.3
- Fix an issue where disconnecting a session using ~. would result in further connections to
  the same initiator would appear to hang.
- Setting `-q` will suppress the pre-connect spinners

### v0.1.2
- Adaptive compression (RNS update) provides significant performance improvements ([PR](https://github.com/acehoss/rnsh/pull/24))
- Allowed identities file - put allowed identities in a file instead of on the command
  line for easier service invocations. ([see PR for details](https://github.com/acehoss/rnsh/pull/25))
- Escape Sequences, Session Termination & Line-Interactive Mode ([see PR for details](https://github.com/acehoss/rnsh/pull/26))

### v0.1.1
- Fix issue with intermittent data corruption

### v0.1.0
- First beta! Includes peformance improvements.

### v0.0.13, v0.0.14
- Bug fixes

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

## Protocol
The protocol is build on top of the Reticulum `Packet` API.
Application software sends and receives `Message` objects,
which are encapsulated by `Packet` objects. Messages are
(currently) sent one per packet, and only one packet is
sent at a time (per link). The next packet is not sent until 
the receiver proves the outstanding packet.

A future update will work to allow a sliding window of
outstanding packets to improve channel utilization.

### Session Establishment
1. Initiator establishes link. Listener session enters state 
   `LSSTATE_WAIT_IDENT`, or `LSSTATE_WAIT_VERS` if running
   with `--no-auth` option.

2. Initiator identifies on link if not using `--no-id`.
   - If using `--allowed-hash`, listener validates identity 
      against configuration and if no match, sends a 
      protocol error message and tears down link after prune
      timer.
3. Initiator transmits a `VersionInformationMessage`, which
   is evaluated by the server for compatibility. If
   incompatible, a protocol error is sent. 
4. Listener responds with a `VersionInfoMessage` and enters 
   state `LSSTATE_WAIT_CMD`
5. Initiator evaluates the listener's version information
   for compatibility and if incompatible, tears down link.
6. Initiator sends an `ExecuteCommandMessage` (which could 
   be an empty command) and enters the session event loop.
7. Listener evaluates the command message against the
   configured options such as `-A` or `-C` and responds
   with a protocol error if not allowed.
8. Listener starts the program. If success, the listener
   enters the session event loop. If failure, responds
   with a `CommandExitedMessage`.

### Session Event Loop
##### Listener state `LSSTATE_RUNNING`
Process messages received from initiator.
- `WindowSizeMessage`: set window size on child tty if appropriate
- `StreamDataMessage`: binary data stream for child process;
  streams ids 0, 1, 2 = stdin, stdout, stderr
- `NoopMessage`: no operation - listener replies with `NoopMessage`
- When link is torn down, child process is terminated if running and 
  session destroyed
- If command terminates, a `CommandExitedMessage` is sent and session
  is pruned after an idle timeout.
##### Initiator state `ISSTATE_RUNNING`
Process messages received from listener.
- `ErrorMessage`: print error, terminate link, and exit
- `StreamDataMessage`: binary stream information; 
   streams ids 0, 1, 2 = stdin, stdout, stderr
- `CommandExitedMessage`: remote command exited
- If link is torn down unexpectedly, print message and exit

   
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
- [X] ~~Fix issues with running `rnsh` in a binary pipeline, i.e. 
  piping the output of `tar` over `rsh`.~~
- [X] ~~Test on several platforms~~
- [X] ~~Fix issues that come up with testing~~
- [X] ~~v0.1.0 beta~~
- [X] ~~Test and fix more issues~~
- [ ] More betas
- [ ] Enhancement Ideas
  - [x] `authorized_keys` mode similar to SSH to allow one listener
        process to serve multiple users
  - [ ] Git over `rnsh` (git remote helper)
  - [ ] Sliding window acknowledgements for improved throughput
- [ ] v1.0 someday probably maybe

## Miscellaneous

By piping into/out of `rnsh`, it is possible to transfer
files using the same method discussed in 
[this article](https://cromwell-intl.com/open-source/tar-and-ssh.html).
It's not terribly fast currently, due to the round-trip rule 
enforced by the protocol. Sliding window acknowledgements will
speed this up significantly.

Running tests: `poetry run pytest tests`