import functools

from twisted.internet import protocol, reactor
from twisted.words.protocols import irc

from omegletwist import OmegleBot


def trace(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        print "%s(%s %s)" % (func.__name__, args, kwargs)
        return func(*args, **kwargs)
    return wrapper


bridge_bot_dispatch = {}  # eg {'/command': command_func}


def command(f):
    @functools.wraps(f)
    def command_wrapper(*args, **kwargs):
        return f(*args, **kwargs)

    bridge_bot_dispatch["/%s" % f.__name__] = f


class BridgeBotProtocol(irc.IRCClient):
    """An irc bot that bridges to Omegle conversations."""

    # attributes set in factory:
    #   active_nickname
    #   idle_nickname
    #   omegle_bot

    idle = False  # hack to force idle on init connect
    piping_user = None
    autoconnect = False

    @command
    def connect(self, *args):
        """Get a stranger to talk to."""
        d = self.omegle_bot.connect()

        def after_connect(connect_info):
            self.goActive()

        d.addCallback(after_connect)

    @command
    def disconnect(self, *args):
        """Disconnect from our current stranger."""
        self.omegle_bot.disconnect()
        self.goIdle()

    @command
    def help(self, *args):
        self.say(self.factory.channel, 'Possible commands:')
        for cmd_name in sorted(bridge_bot_dispatch.keys()):
            self.say(self.factory.channel, "  %s" % cmd_name)

    @command
    def captcha(self, *args):
        self.omegle_bot.solveCaptcha(' '.join(args))

    @command
    def pipe(self, *args):
        if len(args) > 0:
            self.piping_user = args[0]
            print "Piping to %r." % self.piping_user
            self.say(self.factory.channel, "<piping to %r>" % self.piping_user)
        else:
            self.say(self.factory.channel, 'Usage: /pipe <nick>')

    @command
    def unpipe(self, *args):
        self.say(self.factory.channel, "<no longer piping to %r>" % self.piping_user)
        self.piping_user = None

    @command
    def popcorn(self, *args):
        self.say(self.factory.channel, '<will auto-reconnect>')
        self.autoconnect = True

    @command
    def unpopcorn(self, *args):
        self.say(self.factory.channel, '<will not auto-reconnect>')
        self.autoconnect = False

    def goIdle(self):
        if not self.idle:
            self.idle = True
            self.setNick(self.idle_nickname)
            self.away('disconnected')

    def goActive(self):
        if self.idle:
            self.idle = False
            self.setNick(self.active_nickname)
            self.back()

    def signedOn(self):
        self.join(self.factory.channel)
        print "Signed on as %s." % (self.nickname,)

    def joined(self, channel):
        print "Joined %s." % (channel,)
        self.goIdle()

    def privmsg(self, user, channel, msg):
        user = user.split('!')[0]

        if self.piping_user == user and not self.idle:
            print 'bot:', msg.strip()
            self.omegle_bot.say(msg.strip())
            return

        try:
            to, msg_rest = [s.strip() for s in msg.split(':')]
        except ValueError:
            return  # no colon
        else:
            if to != self.nickname or user == self.nickname:
                return

        # someone directed a msg at us; need to respond
        print "<- '%s' (%s)" % (msg, user)

        msg_split = msg_rest.split()
        command_name, args = msg_split[0], msg_split[1:]

        command = bridge_bot_dispatch.get(command_name)
        if command:
            command(self, *args)
        elif not self.idle:
            print 'bot:', msg_rest
            self.omegle_bot.say(msg_rest)

    def typingCallback(self, *args):
        pass

    def stoppedTypingCallback(self, *args):
        pass

    def disconnectCallback(self, *args):
        print 'disconnected'
        print

        self.say(self.factory.channel, '<stranger disconnected>')

        if self.autoconnect:
            bridge_bot_dispatch['/connect'](self)
        else:
            self.goIdle()

    def messageCallback(self, *args):
        msg = args[1][0].encode('utf-8')
        print 'stranger:', msg

        if self.piping_user:
            msg = self.piping_user + ': ' + msg
        self.say(self.factory.channel, msg)

    @trace
    def recaptchaFailedCallback(self, *args):
        self.say(self.factory.channel, '<captcha was incorrect>')

    @trace
    def recaptchaRequiredCallback(self, *args):
        msg = ("<Omegle requires a captcha."
               " Solve it using `/captcha <solutiontext>`."
               " url: %s") % args[1]

        self.say(self.factory.channel, msg)

    def connectCallback(self, *args):
        print 'connected'
        self.say(self.factory.channel, '<stranger connected>')
        self.goActive()

    def waitingCallback(self, *args):
        pass


class BridgeBotFactory(protocol.ClientFactory):
    protocol = BridgeBotProtocol

    def __init__(self, channel, nickname='dev_omgb'):
        self.channel = channel
        self.nickname = nickname

    def buildProtocol(self, *args, **kw):
        prot = protocol.ClientFactory.buildProtocol(self, *args, **kw)
        prot.nickname = self.nickname
        prot.active_nickname = prot.nickname
        prot.idle_nickname = prot.nickname + '_idle'
        prot.omegle_bot = OmegleBot(prot)

        return prot

    def clientConnectionLost(self, connector, reason):
        print "Lost connection (%s), reconnecting." % (reason,)
        connector.connect()

    def clientConnectionFailed(self, connector, reason):
        print "Could not connect: %s" % (reason,)


if __name__ == '__main__':
    reactor.connectTCP('irc.freenode.net',
                       6667,
                       BridgeBotFactory('##rochack'),
                      )
    reactor.run()
