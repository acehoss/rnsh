# `r n s h`  Shell over Reticulum

`rnsh` is a utility written in Python that facilitates shell 
sessions over Reticulum networks. It is based off the `rnx` 
utility that ships with Reticulum.

`rnsh` is still pretty raw; there are some things that are 
implemented badly, and many other things that haven't been 
built at all (yet). Signals (i.e. Ctrl-C) need some work, so have
another terminal handy to send a SIGTERM if things glitch
out.

## Quickstart

Requires Python 3.10+ on Linux or Unix. WSL probably works. Cygwin might work, too.

- Activate a virtualenv
- `pip3 install rnsh`
  - Or from a `whl` release, `pip3 install /path/to/rnsh-0.0.1-py3-none-any.whl`
- Configure Reticulum interfaces, check with `rnstatus`
- Ready to run `rnsh`. The options are shown below.

```
Usage:
    rnsh [--config <configdir>] [-i <identityfile>] [-s <service_name>] [-l] -p
    rnsh -l [--config <configfile>] [-i <identityfile>] [-s <service_name>] 
         [-v...] [-q...] [-b] (-n | -a <identity_hash> [-a <identity_hash>]...) 
         [--] <program> [<arg>...]
    rnsh [--config <configfile>] [-i <identityfile>] [-s <service_name>] 
         [-v...] [-q...] [-N] [-m] [-w <timeout>] <destination_hash>
    rnsh -h
    rnsh --version

Options:
    --config DIR             Alternate Reticulum config directory to use
    -i FILE --identity FILE  Specific identity file to use
    -s NAME --service NAME   Listen on/connect to specific service name if not default
    -p --print-identity      Print identity information and exit
    -l --listen              Listen (server) mode
    -b --no-announce         Do not announce service
    -a HASH --allowed HASH   Specify identities allowed to connect
    -n --no-auth             Disable authentication
    -N --no-id               Disable identify on connect
    -m --mirror              Client returns with code of remote process
    -w TIME --timeout TIME   Specify client connect and request timeout in seconds
    -v --verbose             Increase verbosity
    -q --quiet               Increase quietness
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
   
## Roadmap
1. Plan a better roadmap
2. ?
3. Keep my day job

## TODO
- [X] ~~Initial version~~
- [X] ~~Pip package with command-line utility support~~
- [ ] Publish to PyPI
- [ ] Improve signal handling
- [ ] Protocol improvements (throughput!)
- [ ] Test on several *nixes
- [ ] Make it scriptable (currently requires a tty)
- [ ] Documentation improvements
