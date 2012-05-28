import time
from multiprocessing import Process, current_process
from threading import Thread, current_thread

from pulsar import system, wrap_socket, platform, socket_pair
from pulsar.utils.tools import gen_unique_id

from .iostream import AsyncIOStream
from .proxy import ActorProxyMonitor
from .actor import get_actor
from .defer import pickle


__all__ = ['Concurrency', 'concurrency']


def concurrency(kind, actor_class, timeout, monitor, aid, commands_set, params):
    '''Function invoked by the :class:`Arbiter` when spawning a new
:class:`Actor`.'''
    if kind == 'monitor':
        cls = MonitorConcurrency
    elif kind == 'thread':
        cls = ActorThread
    elif kind == 'process':
        cls = ActorProcess
    else:
        raise ValueError('Concurrency %s not supported by pulsar' % kind)
    return cls.make(kind, actor_class, timeout, monitor, aid,
                    commands_set, params)
    
    
class Concurrency(object):
    '''Actor implementation is responsible for the actual spawning of
actors according to a concurrency implementation. Instances are pickable
and are shared between the :class:`Actor` and its
:class:`ActorProxyMonitor`.

:parameter concurrency: string indicating the concurrency implementation.
    Valid choices are ``monitor``, ``process`` and ``thread``.
:parameter actor_class: :class:`Actor` or one of its subclasses.
:parameter timeout: timeout in seconds for the actor.
:parameter kwargs: additional key-valued arguments to be passed to the actor
    constructor.
'''
    @classmethod
    def make(cls, kind, actor_class, timeout, monitor, aid,
             commands_set, kwargs):
        self = cls()
        if not aid:
            if monitor and monitor.is_arbiter():
                aid = 'arbiter'
            else:
                aid = gen_unique_id()[:8]
        self.aid = aid
        self.impl = kind
        self.commands_set = commands_set
        self.timeout = timeout
        self.actor_class = actor_class
        self.loglevel = kwargs.pop('loglevel',None)
        self.a_kwargs = kwargs
        self.process_actor(monitor)
        return self
       
    @property
    def name(self):
        return '{0}({1})'.format(self.actor_class.code(), self.aid)
     
    def __str__(self):
        return self.name
    
    def proxy_monitor(self):
        return ActorProxyMonitor(self)
    
    def process_actor(self, monitor):
        '''Called at initialization, it set up communication layers for the
actor. In particular here is where the outbox handler is created.'''
        if monitor.is_arbiter():
            arbiter = monitor
        else:
            arbiter = monitor.arbiter
        self.a_kwargs['arbiter'] = arbiter.proxy
        self.a_kwargs['monitor'] = monitor.proxy
    
    
class MonitorConcurrency(Concurrency):
    '''An actor implementation for Monitors. Monitors live in the main process
loop and therefore do not require an inbox.'''
    def process_actor(self, arbiter):
        self.a_kwargs['arbiter'] = arbiter
        self.timeout = 0
        self.actor = self.actor_class(self, **self.a_kwargs)
        
    def proxy_monitor(self):
        return None
    
    def start(self):
        pass
    
    def is_active(self):
        return self.actor.is_alive()
    
    @property    
    def pid(self):
        return current_process().pid


class ActorConcurrency(Concurrency):
    
    def run(self):
        self.actor = get_actor(self.actor_class(self, **self.a_kwargs))
        self.actor.start()
        

class ActorProcess(ActorConcurrency, Process):
    '''Actor on a process'''
    pass
        
        
class ActorThread(ActorConcurrency, Thread):
    '''Actor on a thread'''
    def terminate(self):
        result = self.actor.stop(force=True)
        while result.called:
            time.sleep(0.1)
    
    @property    
    def pid(self):
        return current_process().pid

