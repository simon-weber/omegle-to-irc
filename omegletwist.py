"""
An omegle client that uses Twisted.

Adapted from https://gist.github.com/e000/830484
"""

import random
import re
import string
from urllib import urlencode

from twisted.internet.defer import (
    DeferredLock, inlineCallbacks, CancelledError, returnValue
)

from twisted.web.client import getPage
from json import loads as json_decode

DISCONNECTED = 0
CONNECTING = 1
WAITING = 2
CONNECTED = 3

_userAgents = [
    'Mozilla/5.0 (X11; U; Linux i686; en-US' + tail for tail in (
        '; rv:1.9.2.10) Gecko/20100915 Ubuntu/10.04 (lucid) Firefox/3.6.10',
        (') AppleWebKit/534.16 (KHTML, like Gecko) '
            'Chrome/10.0.648.45 Safari/534.16'),
    )
]


def getRandomUserAgent():
    "Gives us a random user-agent to spoof with!"
    return random.choice(_userAgents)


class AlreadyRunningError(Exception):
    pass


class NotConnectedError(Exception):
    pass


class CaptchaNotRequired(Exception):
    pass


class SendError(Exception):
    pass


class OmegleBot():
    DISCONNECTED = 0
    CONNECTING = 1
    WAITING = 2
    CONNECTED = 3
    _serverRegex = re.compile('\<i?frame src="(.*?)"\>')
    _captchaImageRegex = re.compile(
        '\<center\>'
        '\<img width="\d+" height="\d+" alt="" src="image\?c\=(.*?)"\>'
        '\<\/center\>'
    )

    def __init__(self, omegleProto):
        """
        Initializes an L{OmegleBot}.

        @param omegleProto: an instance of a protocol that implements:
            * typingCallback: when the stranger is typing
            * stoppedTypingCallback: stranger no longer typing
            * disconnectCallback: stranger OR bot has disconnected
            * messageCallback: when the stranger has sent us a message
            * recaptchaFailedCallback: when our submitted captcha fails
            * recaptchaRequiredCallback: when omegle requires a captcha
            * connectCallback: when we have found a stranger
            * waitingCallback: when we are waiting for a stranger
        """

        for callback_name in ('typingCallback',
                              'stoppedTypingCallback',
                              'disconnectCallback',
                              'messageCallback',
                              'recaptchaFailedCallback',
                              'recaptchaRequiredCallback',
                              'connectCallback',
                              'waitingCallback',
                             ):
            setattr(self, callback_name, getattr(omegleProto, callback_name, None))

        self.status = DISCONNECTED
        self.server = None
        self.id = None
        self.lock = DeferredLock()
        self.activeRequests = set()
        self.challenge = None
        self.image = None

    def disconnect(self):
        """Disconnect if we are connected; otherwise, do nothing."""
        if self.status in (WAITING, CONNECTED):
            # | /dev/null
            self.getPage('disconnect',
                         addToActive=False,
                         data={'id': self.id}
                        ).addErrback(lambda r: None)

        if self.status == DISCONNECTED:
            return

        self.status = DISCONNECTED
        self.id = None
        self.challenge = None
        self.server = None
        self._cancelAllRequests()
        self.onDisconnect()

    def _cancelAllRequests(self):
        """
        Kills all active connetions, i/o and empties the message queue
        """
        self.lock.waiting[:] = []
        for d in list(self.activeRequests):
            d.cancel()

        self.activeRequests.clear()

    def getPage(self, url, addToActive=True, data=None, *args, **kwargs):
        """
        Retrieves a page using the twisted getPage function,
        and if addToActive is true,
        will add to the tracked requests that will cancel if we disconnect.
        """
        def removeFromActive(r):
            self.activeRequests.discard(d)
            return r

        if not url.startswith('http://') and self.server:
            url = self.server + url

        if data is not None:
            data = urlencode(data)
            kwargs.update({
                'method': 'POST',
                'postdata': data,
                'headers': {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Content-Length': '%i' % len(data)
                }
            })

        d = getPage(url, agent=self.userAgent, *args, **kwargs)
        if addToActive:
            self.activeRequests.add(d)
            d.addBoth(removeFromActive)

        return d

    def say(self, message):
        """
        send a message to the connected user
        raises NotConnectedError if we're not connected

        @param message: the message to send
        @type message: string, unicode
        """

        if self.status != CONNECTED:
            raise NotConnectedError()

        def sentMessage(response):
            if response == 'win':
                return True
            else:
                raise SendError("Couldn't send message.")

        return self._doLockedCommand(
            'send', data={'id': self.id, 'msg': message}
        ).addCallback(sentMessage)

    def typing(self):
        """
        tells the connected user that we're typing
        raises NotConnectedError if we're not connected
        """
        if self.status != CONNECTED:
            raise NotConnectedError()
        self._doLockedCommand(
            'typing', data={'id': self.id}
        )

    def stoppedTyping(self):
        """
        tells the connected user that we're not typing anymore
        raises NotConnectedError if we're not connected
        """
        if self.status != CONNECTED:
            raise NotConnectedError()
        self._doLockedCommand(
            'stoppedtyping', data={'id': self.id}
        )

    def solveCaptcha(self, solution):
        """
        attempts to solve the captcha that omegle sent to us.

        @param solution: the solution to the captcha
        @type solution: string
        """

        if not self.challenge and self.image:
            raise CaptchaNotRequired()

        self.getPage('recaptcha', data={
            'id': self.id,
            'response': solution,
            'challenge': self.image
        })
        self.image, self.challenge = None, None

    def _doLockedCommand(self, url, data):
        """
        internal command that adds it to our DeferredLock queue,
        which will fire sequentially as they finish,
        allowing only one request to be processed at once
        """
        l = self.lock.acquire()

        def gotLock(lock):
            if self.status == CONNECTED:
                def releaseLock(r):
                    lock.release()
                    return r
                d = self.getPage(url, data=data)
                d.addBoth(releaseLock)
                return d
            else:
                lock.release()

        return l.addCallback(gotLock)

    @staticmethod
    def _get_rand_id():
        """Return a random 8 char all-cap alphanum string, eg '4B5MP9J6'."""
        ''.join(random.choice(string.ascii_uppercase + string.digits) for x in range(8))

    @inlineCallbacks
    def connect(self):
        """
        attempts to connect to the Omegle server.

        returns a deferred that will fire when we've established a connection
        """
        if self.status != DISCONNECTED:
            raise AlreadyRunningError()
        self.userAgent = getRandomUserAgent()
        self.status = CONNECTING

        """
        print 'connecting to omegle...'
        homePage = yield self.getPage('http://omegle.com/')

        print 'got page, searching for server'
        with open('markup.html', 'w') as f:
            f.write(homePage)

        match = self._serverRegex.search(homePage)
        if not match:
            raise ValueError("Could not find a server to connect to!")
        else:
            self.server = match.group(1)
        """

        self.server = 'http://front2.omegle.com/'
        id = yield self.getPage("start?rcs=1&spid=&randid=%s" % self._get_rand_id())

        self.id = json_decode(id)
        self.status = WAITING
        self.doEvents()
        returnValue((self.id, self.server))

    def doEvents(self):
        """
        main asynchronous io loop that handles events, and asks for more
        """
        if self.status not in (CONNECTED, WAITING):
            return

        def gotEvents(response):
            events = json_decode(response)
            if events is None:
                self.disconnect()
            else:
                for event in events:
                    event, params = event[0], event[1:]
                    callback = getattr(self, 'EVENT_%s' % event, None)
                    if callback:
                        callback(params)

                self.doEvents()

        def gotError(error):
            if not isinstance(error.value, CancelledError):
                self.disconnect()
                self.onError(error)

        return self.getPage('events', data={
            'id': self.id
        }).addCallbacks(gotEvents, gotError)

    def EVENT_waiting(self, params):
        """ we received a waiting event """
        self.status = WAITING
        self.runCallback(self.waitingCallback)

    def EVENT_connected(self, params):
        """ we're connected to a partner """
        self.status = CONNECTED
        self.runCallback(self.connectCallback)

    def EVENT_gotMessage(self, params):
        """ partner sent us a message! """
        self.runCallback(self.messageCallback, params)

    def EVENT_typing(self, params):
        """ partner is typing """
        self.runCallback(self.typingCallback)

    def EVENT_stoppedTyping(self, params):
        """ partner stopped typing """
        self.runCallback(self.stoppedTypingCallback)

    def EVENT_strangerDisconnected(self, params):
        """ partner disconnected """
        self.disconnect()

    def doCaptcha(self, challenge):
        """ returns a deferred that will fire when we have the location of the captcha image """
        def gotImage(r):
            self.image = r
            return r

        def error(error):
            self.onError(error)
            self.disconnect()
            return None

        d = self.getRecaptchaImage(challenge)
        d.addCallback(gotImage).addErrback(error)
        return d

    @inlineCallbacks
    def getRecaptchaImage(self, key):
        """ try and find the image to solve """
        page = 'http://www.google.com/recaptcha/api/noscript?'
        pg = yield self.getPage(page + urlencode({'k': key}), headers={
            'referer': 'http://www.omegle.com/'
        })
        match = self._captchaImageRegex.search(pg)
        if match:
            returnValue(match.group(1))
        else:
            raise ValueError("Could not find the image!")

    def EVENT_recaptchaRequired(self, params):
        """ omegle says we need a captcha to connect """
        #params = challenge,
        self.challenge = params[0]
        params.append(self.doCaptcha(self.challenge))
        self.runCallback(self.recaptchaRequiredCallback, params)

    def EVENT_recaptchaRejected(self, params):
        """ omegle says that our captcha was wrong! """
        self.challenge = params[0]
        params.append(self.doCaptcha(self.challenge))
        self.runCallback(self.recaptchaFailedCallback, params)

    def onDisconnect(self):
        """ we've disconnected """
        self.runCallback(self.disconnectCallback)

    def onError(self, error):
        """ an error has happened! """
        error.printBriefTraceback()

    def runCallback(self, callback, params=None):
        """ run our callback if it's set """
        if callback is None:
            return
        try:
            callback(self, params)
        except:
            from twisted.python import failure
            failure.Failure().printBriefTraceback()
