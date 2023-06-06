import os
import re
import io
import contextlib
import time
import random
import uuid
import signal
import pickle
import subprocess
import traceback
import logging
import argparse
import hashlib

import sqlite3

from . import utils
from .utils import BaseClass
from .config import Config
from .environment import get_environ
from .scheduler import get_scheduler
from .provider import get_provider


task_states = ['WAITING',       # Waiting for requires to finish
               'PENDING',       # Eligible to be selected and run
               'RUNNING',       # Running right now
               'SUCCEEDED',     # Finished with err code = 0
               'FAILED',        # Finished with err code != 0
               'KILLED',        # Finished with SIGTERM (eg Slurm job timeout)
               'UNKNOWN']       # Something went wrong and we lost track


TaskState = type('TaskState', (), {**dict(zip(task_states, task_states)), 'ALL': task_states})


class Task(BaseClass):

    _attrs = ['id', 'app', 'args', 'kwargs', 'args_require_ids', 'kwargs_require_ids', 'state', 'jobid', 'errno', 'err', 'out', 'result', 'dtime']

    def __init__(self, app, args=None, kwargs=None, id=None, state=None):
        for name in ['jobid', 'err', 'out']:
            setattr(self, name, '')
        self.result = None
        self.dtime = None
        self.update(app=app, args=args, kwargs=kwargs, id=id, state=state)

    def update(self, **kwargs):
        if 'args' in kwargs:
            self.args = list(kwargs.pop('args') or ())
            self.args_require_ids = {}
            for iarg, arg in enumerate(self.args):
                if isinstance(Task, arg):
                    self.args_require_ids[iarg] = self.args[iarg] = arg.id
        if 'kwargs' in kwargs:
            self.kwargs = dict(kwargs.pop('kwargs') or {})
            self.kwargs_require_ids = {}
            for key, value in self.kwargs:
                if isinstance(Task, value):
                    self.kwargs_require_ids[key] = self.kwargs[key] = value.id
        if 'state' in kwargs:
            self.state = kwargs.pop('state')
            if self.state is None:
                if self.args_require_ids or self.kwargs_require_ids:
                    self.state = TaskState.WAITING
                else:
                    self.state = TaskState.PENDING
        if 'id' in kwargs:
            id = kwargs.pop('id')
            if id is None:
                uid = pickle.dumps((self.app, self.args, self.kwargs))
                hex = hashlib.md5(uid).hexdigest()
                id = uuid.UUID(hex=hex)  # unique ID, tied to the given app, args and kwargs
            self.id = str(id)
        for name in ['app', 'jobid', 'err', 'out', 'result', 'dtime']:
            if name in kwargs:
                setattr(self, name, kwargs.pop(name))
        if kwargs:
            raise ValueError('Unrecognized arguments {}'.format(kwargs))

    def clone(self, *args, **kwargs):
        new = self.copy()
        new.update(*args, **kwargs)
        return new

    @property
    def require_ids(self):
        return list(set(self.args_require_ids.values()) & set(self.kwargs_require_ids.values()))

    def update_args(self, queue):
        for iarg, id in self.args_require_ids.items():
            self.args[iarg] = queue[id].result
        for key, id in self.kwargs_require_ids.items():
            self.kwargs[key] = queue[id].result

    def run(self):
        t0 = time.time()
        self.errno, self.result, self.err, self.out = self.app.run(tuple(self.args), self.kwargs)
        if self.errno:
            if self.errno == signal.SIGTERM:
                self.state = TaskState.KILLED
            else:
                self.state = TaskState.FAILED
        self.dtime = time.time() - t0

    def __getstate__(self):
        return {name: getattr(self, name) for name in self._attrs}


class Future(BaseClass):

    def __init__(self, queue, id):
        self.queue = queue
        self.id = str(id)

    def result(self, timeout=1e4, timestep=10.):
        t0 = time.time()
        try:
            return self._result
        except AttributeError:
            while True:
                if (time.time() - t0) < timeout:
                    if self.queue.tasks(id=self.id, property='state')[0] not in (TaskState.WAITING, TaskState.PENDING):
                        self._result = self.queue.tasks(self.id)[0].result
                        return self._result
                    time.sleep(timestep * random.uniform(0.8, 1.2))
                else:
                    self.log_error('time out while getting result', flush=True)
                    return None


queue_states = ['ACTIVE', 'PAUSED']


QueueState = type('QueueState', (), {**dict(zip(queue_states, queue_states)), 'ALL': queue_states})


class Queue(BaseClass):

    def __init__(self, name, base_dir=None, create=None, spawn=False):
        if isinstance(name, self.__class__):
            self.__dict__.update(name.__dict__)
            return

        if base_dir is None:
            base_dir = Config().queue_dir

        if not re.test(name, '^[a-zA-z0-9-_]+$'):
            raise ValueError('Input queue name  must be alphanumeric plus underscores and hyphens')

        self.fn = os.path.join(base_dir, name, 'queue.sqlite')
        self.base_dir = os.path.dirname(self.fn)

        # Check if it already exists and/or if we are supposed to create it
        exists = os.path.exists(self.fn)
        if create is None:
            create = not exists
        elif create and exists:
            raise ValueError('Queue {} already exists'.format(name))
        elif (not create) and (not exists):
            raise ValueError('Queue {} does not exist'.format(name))

        # Create directory with rwx for user but no one else
        if create:
            utils.mkdir(self.base_dir, mode=0o700)
            self.db = sqlite3.Connection(self.fn)

            # Give rw access to user but no one else
            os.chmod(self.fn, 0o600)

            # Create tables
            script = """
            CREATE TABLE tasks (
                id       TEXT PRIMARY KEY,
                task     TEXT,
                state    TEXT,
                tmid     TEXT  -- task manager id
            }
            -- Dependencies table.  Multiple entries for multiple deps.
            CREATE TABLE requires (
                id      TEXT,     -- task.id foreign key
                require TEXT,     -- task.id that it depends upon
            -- Add foreign key constraints
                FOREIGN KEY(id) REFERENCES tasks(id),
                FOREIGN KEY(requires) REFERENCES tasks(id)
            );
            -- Task manager table
            CREATE TABLE managers (
                tmid    TEXT PRIMARY KEY, -- task manager id foreign key
                manager TEXT,             -- task manager
            -- Add foreign key constraints
                FOREIGN KEY(tmid) REFERENCES tasks(tmid)
            );
            -- Metadata about this queue, e.g. active/paused
            CREATE TABLE metadata (
                key   TEXT,
                value TEXT
            );
            """
            self.db.executescript(script)
            # Initial queue state is active
            self.db.execute('INSERT INTO metadata VALUES (?,?)', ('state', QueueState.ACTIVE))
            self.db.commit()
        else:
            self.db = sqlite3.Connection(self.fn, timeout=60)
        if spawn:
            subprocess.Popen('python desipipe spawn --queue {}'.format(self.base_dir), start_new_session=True)

    def _query(self, query, timeout=120., timestep=2., many=False):
        """
        Perform a database query retrying if needed. If timeout seconds
        pass, then re-raise sqlite3.OperationalError from locked db.

        If ``many``, calls db.executemany(query, args) instead of
        db.execute(query).

        Returns result of query.
        """
        t0, ntries = time.time(), 1
        if isinstance(query, str):
            query = (query,)
        while True:
            try:
                if ntries > 1:
                    self.log_debug('Retrying: "{}"'.format(' '.join(query)))

                if many:
                    result = self.db.executemany(*query)
                else:
                    result = self.db.execute(*query)

                if ntries > 1:
                    self.log_debug('Succeeded after {} tries'.format(ntries))

                return result

            except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
                # A few known errors that can occur when multiple clients
                # are hammering on the database. For these cases, wait
                # and try again a few times before giving up.
                known_excs = ['database is locked', 'database disk image is malformed']  # on NFS
                if exc.message.lower() in known_excs:
                    if (time.time() - t0) < timeout:
                        self.db.close()
                        time.sleep(timestep * random.uniform(0.8, 1.2))
                        self.db = sqlite3.Connection(self.fn)
                        ntries += 1
                    else:
                        self.log_error('tried {} times and still getting errors'.format(ntries))
                        raise exc
                else:
                    raise exc

    def _add_requires(self, id, requires):
        """
        Add requires for a task.

        id : string task id
        requires : list of ids upon which taskid depends
        """
        query = 'INSERT INTO requires (id, require) VALUES (?, ?)'
        if isinstance(requires, str):
            self._query([query, (id, requires)])
        else:
            args = [(id, x) for x in requires]
            self._query([query, args], many=True)

        self.db.commit()

    def _add_manager(self, manager):
        query = 'INSERT INTO managers (tmid, manager) VALUES (?, ?)'
        self._query([query, (manager.id, manager)])
        self.db.commit()

    def add(self, tasks, task_manager=None, replace=False):
        isscalar = isinstance(tasks, Task)
        if isscalar:
            tasks = [tasks]
        ids, requires, managers, states, tasks_serialized, futures = [], [], [], [], [], []
        for task in tasks:
            if task_manager is not None:
                task = task.clone(task_manager=task_manager)
            ids.append(task.id)
            requires.append(task.require_ids)
            managers.append(task.task_manager)
            states.append(task.state)
            tasks_serialized.append(pickle.dumps(task))
            futures.append(Future(queue=self, id=task.id))
        query = 'INSERT'
        if replace:
            query = 'REPLACE'
        if replace is None:
            query = 'INSERT OR REPLACE'
        query += ' INTO tasks (id, task, state, tmid) VALUES (?,?,?,?)'
        self._query([query, zip(ids, tasks_serialized, states, [tm.id for tm in managers])], many=True)
        self.db.commit()
        if not replace:
            for id, requires in zip(ids, requires):
                self._add_requires(id, requires)
            for manager in managers:
                self._add_manager(manager)
        if isscalar:
            return futures[0]
        return futures

    # Get and release locks on the data base
    def _get_lock(self, timeout=10., timestep=2.):
        t0 = time.time()
        while True:
            try:
                self.db.execute('BEGIN IMMEDIATE')
                return True
            except sqlite3.OperationalError:
                if (time.time()  - t0) > timeout:
                    self.log_error('unable to get database lock', flush=True)
                    return False
                time.sleep(timestep * random.uniform(0.8, 1.2))

    def _release_lock(self):
        self.db.commit()

    # Get / Set state of the queue
    @property
    def state(self):
        state = self.db('SELECT value FROM metadata WHERE key="queue_state"').fetchone()[0]
        return state

    @state.setter
    def state(self, state):
        if state not in (QueueState.ACTIVE, QueueState.PAUSED):
            raise ValueError('Invalid queue state {}; should be {} or {}'.format(state, QueueState.ACTIVE, QueueState.PAUSED))
        self._query(['UPDATE metadata SET value=? where key="state"', (state,)])
        self.db.commit()

    def set_task_state(self, id, state):
        self._get_lock()
        try:
            query = 'UPDATE tasks SET state=? WHERE id=?'
            self._query([query, (state, id)])
            self.db.commit()

            if state in (Task.SUCCEEDED, Task.FAILED):
                self._update_waiting_tasks(id)

        except Exception as exc:
            self._release_lock()
            raise exc

        self.db.commit()
        self._release_lock()

    #- functions to update states based on requires
    def _update_waiting_task_state(self, id, force=False):
        """
        Check if all requires of this task have finished running.
        If so, set it into the pending state.

        if force, do the check no matter what. Otherwise, only proceed
        with check if the task is still in the Waiting state.
        """
        if not self._get_lock():
            self.log_error('unable to get db lock; not updating waiting task state')

        # Ensure that it is still waiting
        # (another process could have moved it into pending)
        if not force:
            q = 'SELECT state FROM tasks where tasks.id = ?'
            row = self.db.execute(q, (id,)).fetchone()
            if row is None:
                self._release_lock()
                raise ValueError('Task ID {} not found'.format(id))
            if row[0] != TaskState.WAITING:
                self._release_lock()
                return

        # Count number of requires that are still pending or waiting
        query = """\
        SELECT COUNT(d.requires)
        FROM requires d JOIN tasks t ON d.requires = t.id
        WHERE d.id = ? AND t.state IN ("{}", "{}", "{}")
        """.format(Task.PENDING, Task.WAITING, Task.RUNNING)
        row = self._query([query, (id,)]).fetchone()
        if row is None:
            self._release_lock()
            return
        if row[0] == 0:
            self.set_task_state(id, TaskState.PENDING)
        elif force:
            self.set_task_state(id, TaskState.WAITING)

        self._release_lock()

    def _update_waiting_tasks(self, id):
        """
        Identify tasks that are waiting for id, and call
        :meth:`_update_waiting_task_state` on them.
        """
        query = """\
        SELECT id FROM tasks t JOIN requires d ON d.id = t.id
        WHERE d.requires = ? AND t.state = "{}"
        """.format(TaskState.WAITING)
        waiting_tasks = self._query([query, (id,)]).fetchall()
        for id in waiting_tasks:
            self._update_waiting_task_state(id)

    def pause(self):
        self.state = QueueState.PAUSED

    @property
    def paused(self):
        return self.state == QueueState.PAUSED

    def resume(self):
        self.state = QueueState.ACTIVE

    def delete(self):
        self.db.close()
        del self.db
        import shutil
        # ignore_errors = True is needed on NFS systems; this might
        # leave a dangling directory, but the sqlite db file itself
        # should be gone so it won't appear with qdo list.
        shutil.rmtree(self.base_dir, ignore_errors=True)

    # Get a task
    def pop(self, tmid=None, id=None):
        # First make sure we aren't paused
        if self.state == QueueState.PAUSED:
            return None

        if self._get_lock():
            task = self.tasks(id=id, tmid=tmid, state=TaskState.PENDING, one=True)
        else:
            self.log_warning("There may be tasks left in queue but I couldn't get lock to see")
            return None
        if task is None:
            return None
        self.set_task_state(task.id, TaskState.RUNNING)
        self._release_lock()
        task.update(state=TaskState.RUNNING)
        task.update_args(self)
        return task

    def tasks(self, id=None, tmid=None, state=None, one=False, property=None):
        # View as list
        select = []
        if id is not None:
            select.append('id="{}"'.format(id))
        if tmid is not None:
            select.append('tmid="{}"'.format(tmid))
        if state is not None:
            select.append('state="{}"'.format(state))
        query = 'SELECT task, id, state, tmid FROM tasks'
        if select: query += 'WHERE {}'.format(' AND '.join(select))
        tasks = self._query(query)
        if one: tasks = tasks.fetchone()
        else: tasks = tasks.fetchall()
        if tasks is None:
            if one:
                return None
            return []
        toret = []
        for task in tasks:
            task, id, state, tmid = task
            if property == 'id':
                toret.append(id)
                continue
            if property == 'state':
                toret.append(state)
                continue
            task_manager = self.managers(tmid=tmid)
            if property == 'task_manager':
                toret.append(task_manager)
                continue
            task = pickle.loads(task)
            task.update(id=id, state=state, task_manager=task_manager)
            toret.append(task)
        if one:
            return toret[0]
        return toret

    def managers(self, tmid=None, property=None):
        query = 'SELECT tmid, manager FROM managers'
        one = tmid is not None
        if one:
            query += ' WHERE tmid="{}"'.format(tmid)
        managers = self._query(query).fetchall()
        if managers is None:
            return None
        toret = []
        for manager in managers:
            tmid, manager = manager
            if property in ('tmid', 'id'):
                toret.append(tmid)
                continue
            toret.append(pickle.loads(manager))
        if one:
            return toret[0]
        return toret

    def counts(self, tmid=None, state=None):
        select = []
        if tmid is not None:
            select.append('tmid="{}"'.format(tmid))
        if state is not None:
            select.append('state="{}"'.format(state))
        query = 'SELECT count(state) FROM tasks'
        if select: query += 'WHERE {}'.format(' AND '.join(select))
        return self._query(query).fetchone()[0]

    def summary(self, tmid=None, return_type='dict'):
        counts = {state: self.counts(tmid=tmid, state=state) for state in TaskState.ALL}
        if return_type == 'dict':
            return counts
        if return_type == 'str':
            toret = ''
            for state, count in counts.items():
                toret += '\n{:10s}: {}'.format(state, count)
            return toret
        raise ValueError('Unkown return_type {}'.format(return_type))

    def __repr__(self):
        return '{}(size={}, state={}, fn={})'.format(self.__class__.__name__, self.counts(), self.state, self.fn)

    def __str__(self):
        return self.__repr__() + '\n' + self.summary(return_type='str')

    def __getitem__(self, id):
        toret = self.tasks(id=id)
        if toret is None:
            raise KeyError('task {} not found'.format(id))
        return toret[0]


class BaseApp(BaseClass):

    def __init__(self, func, task_manager=None):
        self.func = func
        self.task_manager = task_manager

    def __call__(self, *args, **kwargs):
        return self.task_manager.add(Task(self, args, kwargs))

    def __getstate__(self):
        return {'func': self.func}


@contextlib.contextmanager
def change_environ(environ):
    """
    Temporarily set the process environment variables.

    >>> with set_env(PLUGINS_DIR='test/plugins'):
    ...   "PLUGINS_DIR" in os.environ
    True

    >>> "PLUGINS_DIR" in os.environ
    False

    :type environ: dict[str, unicode]
    :param environ: Environment variables to set
    """
    if environ is None:
        yield
    environ_bak = os.environ.copy()
    os.environ.clear()
    os.environ.update(environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(environ_bak)


class PythonApp(BaseApp):

    def run(self, args, kwargs, environ=None):
        errno, result = 0, None
        with io.StringIO() as out, io.StringIO() as err, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), change_environ(environ):
            try:
                result = self.func(*args, **kwargs)
            except Exception as exc:
                errno = getattr(exc, 'errno', 42)
                err.write(''.join(traceback.format_stack()))
            return errno, result, err.getvalue(), out.getvalue()


class BashApp(BaseApp):

    def run(self, args, kwargs, environ=None):
        errno, result, out, err = 0, None, '', ''
        cmd = self.func(*args, **kwargs)
        try:
            proc = subprocess.Popen(cmd, shell=True, env=environ)
            out, err = proc.communicate()
        except Exception as exc:
            err += ''.join(traceback.format_stack())
        return errno, result, err, out


class TaskManager(BaseClass):

    def __init__(self, queue, id=None, environ=None, scheduler=None, provider=None):
        self.queue = Queue(queue)
        self.update(id=id, environ=environ, scheduler=scheduler, provider=provider)

    def update(self, **kwargs):
        for name, func in zip(['environ', 'scheduler', 'provider'],
                              [get_environ, get_scheduler, get_provider]):
            if name in kwargs:
                setattr(self, name, func(kwargs.pop(name)))
        if kwargs:
            raise ValueError('Unrecognized arguments {}'.format(kwargs))
        if 'id' in kwargs:
            id = kwargs.pop('id')
            if id is None:
                uid = pickle.dumps((self.environ, self.scheduler, self.provider))
                hex = hashlib.md5(uid).hexdigest()
                id = uuid.UUID(hex=hex)  # unique ID, tied to the given environ, scheduler, provider
            self.id = str(id)

    def clone(self, **kwargs):
        new = self.copy()
        new.update(**kwargs)
        return new

    def python_app(self, func):
        return PythonApp(func, task_manager=self)

    def bash_app(self, func):
        return BashApp(func, task_manager=self)

    def add(self, task, replace=None):
        return self.queue.add(task, task_manager=self, replace=replace)

    def spawn(self, *args, **kwargs):
        self.scheduler(*args, **kwargs)


def work(queue, tmid=None, id=None, mpicomm=None):
    if mpicomm is None:
        from mpi4py import MPI
        mpicomm = MPI.COMM_WORLD
    while True:
        task = None
        if mpicomm.rank == 0:
            task = queue.pop(tmid=tmid, id=id)
        task = mpicomm.bcast(task, root=0)
        if task is None:
            break
        task.run()
        task.update(jobid=os.environ.get('DESIPIPE_JOBID', ''))
        if mpicomm.rank == 0:
            queue.add(task, replace=True)


def spawn(queue, timeout=1e4, timestep=10.):
    queues = [queue] if isinstance(queue, Queue) else queue
    t0 = time.time()
    while True:
        if (time.time() - t0) < timeout:
            break
        if all(queue.paused for queue in queues):
            break
        for queue in queues:
            if queue.paused:
                continue
            for manager in queue.managers(property='id'):
                ntasks = queue.counts(tmid=manager.id, state=TaskState.PENDING)
                manager.spawn('python desipipe work --queue {} --tmid {}'.format(queue.name, manager.id), ntasks=ntasks)
        time.sleep(timestep * random.uniform(0.8, 1.2))


def get_queue(queue, create=False, spawn=False):
    splits = queue.split('/', maxsplit=1)
    if len(splits) == 1:
        users, queues = Config.default_user, splits[0]
    else:
        users, queues = splits
    if '*' in users:
        toret = []
        for f in os.scandir(Config().base_queue_dir):
            if f.is_dir():
                tmp = get_queue('{}/{}'.format(f.name, queues), create=create, spawn=spawn)
                if isinstance(tmp, Queue):
                    toret.append(tmp)
                else:
                    toret += tmp
        return toret
    if '*' in queues:
        toret = []
        for f in os.scandir(Config(user=users).queue_dir):
            if f.is_dir():
                toret.append(get_queue('{}/{}'.format(users, f.name), create=create, spawn=spawn))
        return toret
    return Queue(queues, base_dir=Config(user=users).queue_dir, create=create, spawn=spawn)


def action_from_args(action='work', args=None):

    logger = logging.getLogger('desipipe')
    from .utils import setup_logging

    setup_logging()

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    if action == 'work':

        parser.add_argument('-q', '--queue', type=str, required=True, help='Name of queue; user/queue to select user != {}'.format(Config.default_user))
        parser.add_argument('--tmid', type=str, required=False, default=None, help='Task manager ID')
        parser.add_argument('--id', type=str, required=False, default=None, help='Task ID')
        args = parser.parse_args(args=args)
        if '*' in args.queue:
            raise ValueError('Provide single queue!')
        return work(get_queue(args.queue), tmid=args.tmid, id=args.id)

    if action == 'queues':

        parser.add_argument('-q', '--queue', type='*', required=False, help='Name of queue; user/queue to select user != {} and e.g. */* to select all queues of all users)'.format(Config.default_user))
        args = parser.parse_args(args=args)
        queues = get_queue(args.queue)
        if not queues:
            logger.info('No matching queue')
        logger.info('Matching queues:\n')
        for queue in queues:
            logger.info(str(queue))

    if action == 'tasks':

        parser.add_argument('-q', '--queue', type=str, required=True, help='Name of queue; user/queue to select user != {}'.format(Config.default_user))
        parser.add_argument('--tmid', type=str, required=False, default=None, help='Task manager ID')
        parser.add_argument('--id', type=str, required=False, default=None, help='Task ID')
        parser.add_argument('--state', type=str, required=False, default=TaskState.FAILED, choices=TaskState.ALL, help='Task state')
        args = parser.parse_args(args=args)
        if '*' in args.queue:
            raise ValueError('Provide single queue!')
        logger.info('Tasks that are {}:\n'.format(args.state))
        for task in get_queue(args.queue).tasks(state=args.state, tmid=args.tmid, id=args.id):
            for name in ['jobid', 'errno', 'err', 'out']:
                logger.info('{}: {}\n'.format(name, getattr(task, name)))
            logger.info('=' * 20)

    parser.add_argument('-q', '--queue', nargs='*', type=str, required=True, help='Name of queue; user/queue to select user != {} and e.g. */* to select all queues of all users)'.format(Config.default_user))

    if action == 'delete':

        parser.add_argument('--force', type=bool, required=True, default=False, help='Pass this flag to force delete')
        args = parser.parse_args(args=args)
        queues = get_queue(args.queue)
        if not queues:
            logger.info('No queue to delete')
        logger.info('I will delete these queues:\n')
        for queue in queues:
            logger.info(str(queue))
        if not args.force:
            logger.warning('--force is not set. To actually delete the queues, pass --force')
            return
        for queue in queues:
            queue.delete()

    if action == 'pause':

        args = parser.parse_args(args=args)
        queues = get_queue(args.queue)
        for queue in queues:
            logger.info('Pausing queue {}'.format(repr(queue)))
            queue.pause()
        return

    if action == 'resume':

        args = parser.parse_args(args=args)
        queues = get_queue(args.queue)
        for queue in queues:
            logger.info('Resuming queue {}'.format(repr(queue)))
            queue.resume()
        return

    if action == 'spawn':

        parser.add_argument('--timeout', type=float, required=False, default=1e4, help='Stop after this time')
        args = parser.parse_args(args=args)
        queues = get_queue(args, single=False)
        return spawn(queues, timeout=args.timeout)

    if action == 'retry':

        parser.add_argument('--tmid', type=str, required=False, default=None, help='Task manager ID')
        parser.add_argument('--id', type=str, required=False, default=None, help='Task ID')
        parser.add_argument('--state', type=str, required=False, default=TaskState.KILLED, choices=TaskState.ALL, help='Task state')
        args = parser.parse_args(args=args)
        queues = get_queue(args, single=False)
        for queue in queues:
            for id in queue.tasks(state=args.state, property='ids'):
                queue.set_task_state(id, state=TaskState.PENDING)


action_from_args.actions = {
    'spawn': 'Spawn a manager process to distribute tasks among workers (if queue is not paused)',
    'pause': 'Pause a queue: all workers and managers of provided queues (exclusively) stop after finishing their current task',
    'resume': 'Restart a queue: running manager processes (if any running) distribute again work among workers',
    'delete': 'Delete queue and data base',
    'queues': 'List all queues',
    'tasks': 'List all (failed) tasks of given queue',
    'retry': 'Move all (killed) tasks into PENDING state, so they are rerun'
}