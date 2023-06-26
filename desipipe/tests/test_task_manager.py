import os
import time

from desipipe import Queue, Environment, TaskManager, FileManager


base_dir = './_tests/'


def test_app():

    from desipipe.task_manager import PythonApp

    def func(a, b):
        import numpy as np
        return a * b

    app = PythonApp(func)
    print(app.run((1, 1), {}))


def test_queue():

    spawn = True
    queue = Queue('test', base_dir=base_dir, spawn=spawn)
    provider = None
    if os.getenv('NERSC_HOST', None):
        provider = dict(time='00:02:00', nodes_per_worker=0.1)
    tm = TaskManager(queue, environ=dict(), scheduler=dict(max_workers=2), provider=provider)
    tm2 = tm.clone(scheduler=dict(max_workers=1), provider=dict(provider='local'))

    @tm.python_app
    def fraction(size=10000):
        import time
        import numpy as np
        time.sleep(5)
        x, y = np.random.uniform(-1, 1, size), np.random.uniform(-1, 1, size)
        return np.sum((x**2 + y**2) < 1.) * 1. / size

    @tm2.bash_app
    def echo(fractions):
        return ['echo', '-n', 'these are all results: {}'.format(fractions)]

    @tm2.python_app
    def average(fractions):
        import numpy as np
        return np.average(fractions) * 4.

    t0 = time.time()
    fractions = [fraction(size=1000 + i) for i in range(5)]
    ech = echo(fractions)
    avg = average(fractions)
    if spawn:
        print(ech.out())
        print(avg.result(), time.time() - t0)


def test_cmdline():

    import subprocess
    queue = "'./_tests/*'"
    queue_single = "./_tests/test.sqlite"
    subprocess.call(['desipipe', 'queues', '-q', queue])
    subprocess.call(['desipipe', 'tasks', '-q', queue_single, '--state', 'SUCCEEDED'])
    subprocess.call(['desipipe', 'delete', '-q', queue])
    subprocess.call(['desipipe', 'pause', '-q', queue])
    subprocess.call(['desipipe', 'resume', '-q', queue])
    subprocess.call(['desipipe', 'spawn', '-q', queue])
    subprocess.call(['desipipe', 'retry', '-q', queue, '--state', 'SUCCEEDED', '--spawn'])


def test_file():

    txt = 'hello world!'

    fm = FileManager()
    fm.db.append(dict(description='added file', id='input', filetype='text', path=os.path.join(base_dir, 'hello_in_{i:d}.txt'), options={'i': range(10)}))
    for fi in fm:
        fi.get().write(txt)
    fm.db.append(fm.db[0].clone(id='output', path=os.path.join(base_dir, 'hello_out_{i:d}.txt')))

    spawn = True
    queue = Queue('test2', base_dir=base_dir, spawn=spawn)
    provider = None
    if os.getenv('NERSC_HOST', None):
        provider = dict(time='00:02:00', nodes_per_worker=0.1)
    tm = TaskManager(queue, environ=dict(), scheduler=dict(max_workers=2), provider=provider)

    @tm.python_app
    def copy(text_in, text_out):
        text = text_in.read()
        text += ' this is my first message'
        print('saving', text_out.rpath)
        text_out.write(text)

    results = []
    for fi in fm:
        results.append(copy(fi.get(id='input'), fi.get(id='output')))

    if spawn:
        for res in results:
            print('out', res.out())
            print('err', res.err())


if __name__ == '__main__':

    #test_app()
    #test_queue()
    #test_cmdline()
    test_file()