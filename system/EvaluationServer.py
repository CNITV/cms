#!/usr/bin/python
# -*- coding: utf-8 -*-

# Programming contest management system
# Copyright © 2010-2011 Giovanni Mascellani <mascellani@poisson.phc.unipi.it>
# Copyright © 2010-2011 Stefano Maggiolo <s.maggiolo@gmail.com>
# Copyright © 2010-2011 Matteo Boscariol <boscarim@hotmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import time

import threading
import heapq
import xmlrpclib
import signal
import couchdb

from RPCServer import RPCServer
import Configuration
import Utils
import CouchObject
from View import RankingView


class JobQueue:
    """An instance of this class will contains the (unique) priority
    queue of jobs (compilations, evaluations, ...) that the ES needs
    to process next.

    A job is a couple (job_type, submission), where job_type is a
    constant defined in EvaluationServer.

    Elements of the queue are of the form (priority, timestamp, job),
    where priority is a constant defined in EvaluationServer.
    """

    def __init__(self):
        self.main_lock = threading.RLock()
        self.queue = []
        # The semaphore counts the number of items in the queue
        self.semaphore = threading.Semaphore(0)

    def lock(self):
        self.main_lock.acquire()

    def unlock(self):
        self.main_lock.release()

    def push(self, job, priority, timestamp=None):
        """Push a job in the queue. If timestamp is not specified,
        tries to extract the timestamp from the submission's timestamp
        in the job, or uses the current time.

        job: a couple (job_type, submission)
        priority: the priority of the job
        timestamp: the time of the submission
        """
        if timestamp == None:
            try:
                timestamp = job[1].timestamp
            except:
                timestamp = time.time()
        with self.main_lock:
            heapq.heappush(self.queue, (priority, timestamp, job))
        self.semaphore.release()

    def top(self):
        """Returns the first element in the queue without extracting
        it. If the queue is empty raises an exception.

        returns (job): first element in the queue
        """
        with self.main_lock:
            if len(self.queue) > 0:
                return self.queue[0]
            else:
                raise LookupError("Empty queue")

    def pop(self):
        """Extracts (and returns) the first element in the queue.

        returns (job): first element in the queue
        """
        self.semaphore.acquire()
        with self.main_lock:
            try:
                return heapq.heappop(self.queue)
            except IndexError:
                Utils.log("Job queue went out-of-sync with semaphore",
                          Utils.Logger.SEVERITY_CRITICAL)
                raise RuntimeError("Job queue went out-of-sync with semaphore")

    def search(self, job):
        """Returns a specific job in the queue, if present. If not,
        raises an exception.

        returns (priority, timestamp, job): the data corresponding to
                                            job
        """
        with self.main_lock:
            for i in self.queue:
                # FIXME - Is this `for' correct?
                if self.queue[i][2] == job:
                    return i
        raise LookupError("Job not present in queue")

    # FIXME - Add semaphore handling
    #def delete_by_position(self, pos):
    #    with self.main_lock:
    #        self.queue[pos] = self.queue[-1]
    #        self.queue = self.queue[:-1]
    #        heapq.heapify(self.queue)

    # FIXME - Add semaphore handling
    #def delete_by_job(self, job):
    #    with self.main_lock:
    #        pos = self.search(job)
    #        self.delete_by_position(pos)

    def set_priority(self, job, priority):
        """Change the priority of a job inside the queue.

        Used (only?) when the user use a token, to increase the
        priority of the evaluation of its submission.
        """
        with self.main_lock:
            pos = self.search(job)
            self.queue[pos][0] == priority
            heapq.heapify(self.queue)

    def length(self):
        """Returns the number of elements in the queue.

        returns (int): length of the queue
        """
        return len(self.queue)

    def empty(self):
        """Returns if the queue is empty.

        returns (bool): is the queue empty?
        """
        return self.length() == 0


class WorkerPool:
    """This class keeps the state of the workers attached to ES, and
    allow the ES to get a usable worker when it needs it.
    """

    WORKER_INACTIVE = None
    WORKER_DISABLED = "disabled"

    def __init__(self):
        self.workers = {}
        # The semaphore counts the number of _inactive_ workers
        self.semaphore = threading.Semaphore(0)
        self.main_lock = threading.RLock()
        self.start_time = {}
        self.address = {}
        self.error_count = {}
        self.side_data = {}

        # If schedule_disabling is True, then the next time the worker is
        # inactive it will be disabled.
        self.schedule_disabling = {}

    def acquire_worker(self, job, blocking=True, side_data=None):
        """Tries to assign a job to an available worker. If no workers
        are available and the request is non-blocking, then this
        returns None, otherwise this returns the chosen worker.

        job (job): the job to assign to a worker
        blocking (bool): if False we do not wait for a worker to free
        side_data (object): object to attach to the worker for later
                            use

        returns: None if no workers are available and blocking is
                 False, the worker assigned to the job otherwise
        """
        available = self.semaphore.acquire(blocking)
        if not available:
            return None
        with self.main_lock:
            try:
                worker = self.find_worker(self.WORKER_INACTIVE)
            except LookupError:
                Utils.log("Worker array went out-of-sync with semaphore",
                          Utils.Logger.SEVERITY_CRITICAL)
                raise RuntimeError("Worker array went out-of-sync with " +
                                   "semaphore")
            self.workers[worker] = job
            self.start_time[worker] = time.time()
            self.side_data[worker] = side_data
            Utils.log("Worker %d acquired" % worker,
                      Utils.Logger.SEVERITY_DEBUG)
            return worker

    def release_worker(self, worker):
        """To be called to indicate that the job assigned to the
        worker is concluded (in a successfull way or not). It returns
        the side_data associated to the worker for that job.

        Note: if the worker is scheduled to be disabled, then we
        disable it.

        worker (int): the worker to release, as got by acquire_worker

        returns (object): side_data associated to worker
        """
        with self.main_lock:
            if self.workers[worker] == self.WORKER_INACTIVE or \
                    self.workers[worker] == self.WORKER_DISABLED:
                Utils.log("Trying to release worker while it's inactive",
                          Utils.Logger.SEVERITY_IMPORTANT)
                raise ValueError("Trying to release worker " +
                                 "while it's inactive")
            self.start_time[worker] = None
            side_data = self.side_data[worker]
            self.side_data[worker] = None
            if self.schedule_disabling[worker]:
                self.workers[worker] = self.WORKER_DISABLED
                self.schedule_disabling[worker] = False
                Utils.log("Worker %d released and disabled" % worker,
                          Utils.Logger.SEVERITY_DEBUG)
            else:
                self.workers[worker] = self.WORKER_INACTIVE
                self.semaphore.release()
                Utils.log("Worker %d released" % worker,
                          Utils.Logger.SEVERITY_DEBUG)
        return side_data

    def find_worker(self, job):
        with self.main_lock:
            for worker, workerJob in self.workers.iteritems():
                if workerJob == job:
                    return worker
            raise LookupError("No such job")

    def disable_worker(self, n):
        # TODO - Implement disabling or queueing, depending on the
        # current status of the worker
        # FIXME - Verifying blocking and racing
        avail = self.semaphore.acquire(blocking=False)
        if not avail:
            raise ValueError("No inactive workers available")
        with self.main_lock:
            if self.workers[n] != self.WORKER_INACTIVE:
                self.semaphore.release()
                Utils.log("Trying to disable worker while it isn't inactive",
                          Utils.Logger.SEVERITY_IMPORTANT)
                raise ValueError("Trying to release worker while " +
                                 "it isn't inactive")
            self.workers[n] = self.WORKER_DISABLED
        Utils.log("Worker %d disabled" % n, Utils.Logger.SEVERITY_DEBUG)

    def enable_worker(self, n):
        with self.main_lock:
            if n not in self.workers.keys():
                Utils.log("Trying to enable a non existing worker (`%s')" % n,
                          Utils.Logger.SEVERITY_IMPORTANT)
                raise ValueError("Trying to enable a non existing worker")
            if self.workers[n] != self.WORKER_DISABLED:
                Utils.log("Trying to enable worker while is isn't disabled",
                          Utils.Logger.SEVERITY_IMPORTANT)
                raise ValueError("Trying to enable worker while " +
                                 "it is isn't disabled")
            self.workers[n] = self.WORKER_INACTIVE
        self.semaphore.release()
        Utils.log("Worker %d enabled" % n, Utils.Logger.SEVERITY_DEBUG)

    def add_worker(self, n, host, port):
        with self.main_lock:
            if n in self.workers:
                Utils.log("There is already a worker with name `%s'" % str(n))
                raise ValueError(("There is already a worker " +
                                  "with name `%s'") % str(n))
            self.workers[n] = self.WORKER_INACTIVE
            self.start_time[n] = None
            self.address[n] = (host, port)
            self.error_count[n] = 0
            self.schedule_disabling[n] = False
            self.side_data[n] = None
        self.semaphore.release()
        Utils.log("Worker %d added with address %s:%d" % (n, host, port),
                  Utils.Logger.SEVERITY_DEBUG)

    def del_worker(self, n):
        with self.main_lock:
            if n not in self.workers:
                Utils.log(("Trying to delete a non existing worker " +
                           "(`%s')") % str(n),
                          Utils.Logger.SEVERITY_IMPORTANT)
                raise ValueError("Trying to delete a non existing worker")
            if self.workers[n] != self.WORKER_DISABLED:
                Utils.log(("Trying to delete a worker while " +
                           "it is not disabled (`%s')") % str(n),
                          Utils.Logger.SEVERITY_IMPORTANT)
                raise ValueError("Trying to delete a worker while " +
                                 "it's not disabled")
            del self.workers[n]
            del self.start_time[n]
            del self.address[n]
            del self.error_count[n]
            del self.schedule_disabling[n]
            del self.side_data[n]
        Utils.log("Worker %d deleted" % n, Utils.Logger.SEVERITY_DEBUG)

    def working_workers(self):
        with self.main_lock:
            return len([x for x in self.workers.values
                        if x != self.WORKER_INACTIVE and \
                            x != self.WORKER_DISABLED])

    def represent_job(self, job):
        if job == None:
            return 'None'
        if isinstance(job, str):
            return job
        else:
            return (job[0], job[1].couch_id)

    def get_workers_status(self):
        return dict([(str(n), {'job': self.represent_job(self.workers[n]),
                               'address': self.address[n],
                               'start_time': self.start_time[n],
                               'error_count': self.error_count[n],
                               'side_data': self.side_data[n]})
                for n in self.workers.keys()])

    def check_timeout(self):
        with self.main_lock:
            now = time.time()
            lost_jobs = []
            for worker in self.workers:
                if self.start_time[worker] != None:
                    active_for = now - self.start_time[worker]
                    if active_for > Configuration.worker_timeout:
                        host, port = self.address[worker]
                        Utils.log(("Disabling and shutting down worker %d " +
                                   "(%s:%d) because of no reponse " +
                                   "in %.2f seconds") %
                                  (worker, host, port, active_for),
                                  Utils.Logger.SEVERITY_IMPORTANT)
                        assert self.workers[worker] != self.WORKER_INACTIVE \
                            and self.workers[worker] != self.WORKER_DISABLED
                        job = self.workers[worker]
                        priority, timestamp = self.side_data[worker]
                        lost_jobs.append((priority, timestamp, job))
                        self.schedule_disabling[worker] = True
                        self.release_worker(worker)
                        p = xmlrpclib.ServerProxy("http://%s:%d" %
                                                  (host, port))
                        try:
                            p.shut_down("No response in %.2f seconds" %
                                        (active_for))
                        except:
                            # No problem, probably the worker already
                            # crashed or got disconnected
                            pass
        return lost_jobs


class JobDispatcher(threading.Thread):
    ACTION_OK, ACTION_FAIL, ACTION_REQUEUE = True, False, "requeue"

    def __init__(self, worker_num):
        threading.Thread.__init__(self)
        self.queue = JobQueue()
        self.workers = WorkerPool()
        for i, w in enumerate(Configuration.workers):
            self.workers.add_worker(i, w[0], w[1])
        self.main_lock = threading.RLock()
        self.bomb_primed = False
        self.touched = threading.Event()
        self.check_thread = threading.Thread(target=self.check_timeout_thread)
        self.check_thread.daemon = True

    def check_timeout(self):
        Utils.log("Check for timeouting workers started",
                  Utils.Logger.SEVERITY_DEBUG)
        lost_jobs = self.workers.check_timeout()
        for priority, timestamp, job in lost_jobs:
            Utils.log(("Requeuing job (%s, %s), lost because of "
                       + "timeouting worker") % (job[0], job[1].couch_id))
            self.queue_push(priority=priority, timestamp=timestamp, job=job)
        Utils.log("Check for timeouting workers finished",
                  Utils.Logger.SEVERITY_DEBUG)

    def check_timeout_thread(self):
        while True:
            time.sleep(Configuration.worker_timeout_check_time)
            self.check_timeout()

    def check_action(self, priority, timestamp, job):
        # FIXME - Update docstring
        """Try to execute the specified action immediately: if it's
        possible, do it and returns True; otherwise returns False.
        """

        action = job[0]

        # A bomb is never accepted and block the queue forever, so no
        # further jobs can be assigned to workers; moreover, schedule
        # another evaluation of the queue, in order to make the bomb
        # explode if workers are already free.
        if action == EvaluationServer.JOB_TYPE_BOMB:
            if not self.bomb_primed:
                Utils.log("Priming the bomb")
                self.bomb_primed = True
                self.touched.set()
            return self.ACTION_FAIL

        else:
            worker = self.workers.acquire_worker(job, blocking=False,
                                                 side_data=(priority,
                                                            timestamp))

            if worker == None:
                return self.ACTION_FAIL

            else:
                submission = job[1]
                queue_time = self.workers.start_time[worker] - timestamp
                host, port = self.workers.address[worker]

                Utils.log(("Asking worker %d (%s:%d) to %s submission %s " +
                           " (after around %.2f seconds of queue)") %
                          (worker,
                           host,
                           port,
                           action,
                           submission.couch_id,
                           queue_time))
                Utils.log("Still %d jobs in the queue" % self.queue.length(),
                          Utils.Logger.SEVERITY_DEBUG)
                try:
                    p = xmlrpclib.ServerProxy("http://%s:%d" % (host, port))
                    if action == EvaluationServer.JOB_TYPE_COMPILATION:
                        p.compile(submission.couch_id)
                    elif action == EvaluationServer.JOB_TYPE_EVALUATION:
                        p.evaluate(submission.couch_id)
                except:
                    Utils.log(("Couldn't contact worker %d (%s:%d), " +
                               "disabling it and requeuing submission %s") %
                              (worker, host, port, submission.couch_id),
                              Utils.Logger.SEVERITY_IMPORTANT)
                    self.workers.error_count[worker] += 1
                    self.workers.release_worker(worker)
                    self.workers.disable_worker(worker)
                    return self.ACTION_REQUEUE

                return self.ACTION_OK

    def process_queue(self):
        with self.main_lock:
            while True:
                try:
                    priority, timestamp, job = self.queue.top()
                except LookupError:
                    # The queue is empty, there is nothing other to
                    # do. Good! :-)
                    return

                res = self.check_action(priority, timestamp, job)
                if res == self.ACTION_OK:
                    self.queue.pop()
                elif res == self.ACTION_FAIL:
                    return
                elif res == self.ACTION_REQUEUE:
                    pass

    def run(self):
        self.check_thread.start()
        while True:
            # FIXME - An atomic wait-and-clear would be better
            self.touched.wait()
            self.touched.clear()
            with self.main_lock:
                if self.bomb_primed and self.workers.working_workers() == 0:
                    Utils.log("KABOOM!!")
                    return
                self.process_queue()

    def queue_push(self, job, priority, timestamp=None):
        with self.main_lock:
            self.queue.push(job, priority, timestamp)
        self.touched.set()

    def queue_set_priority(self, job, priority):
        with self.main_lock:
            self.queue.set_priority(job, priority)
        self.touched.set()

    def release_worker(self, worker):
        with self.main_lock:
            side_data = self.workers.release_worker(worker)
        self.touched.set()
        return side_data

    def find_worker(self, job):
        with self.main_lock:
            return self.workers.find_worker(job)

    def get_workers_status(self):
        with self.main_lock:
            return self.workers.get_workers_status()

    def enable_worker(self, n):
        with self.main_lock:
            self.workers.enable_worker(n)
        self.touched.set()

    def add_worker(self, n, addr, port):
        with self.main_lock:
            self.workers.add_worker(n, addr, port)
        self.touched.set()

    def del_worker(self, n):
        with self.main_lock:
            self.workers.del_worker(n)


class EvaluationServer(RPCServer):
    JOB_PRIORITY_EXTRA_HIGH = 0
    JOB_PRIORITY_HIGH = 1
    JOB_PRIORITY_MEDIUM = 2
    JOB_PRIORITY_LOW = 3
    JOB_PRIORITY_EXTRA_LOW = 4

    JOB_TYPE_COMPILATION = "compile"
    JOB_TYPE_EVALUATION = "evaluate"
    JOB_TYPE_BOMB = "bomb"

    MAX_COMPILATION_TENTATIVES = 3
    MAX_EVALUATION_TENTATIVES = 3

    def __init__(self, contest, listen_address=None, listen_port=None):
        Utils.log("Evaluation Server for contest %s started..." %
                  (contest.couch_id))

        if listen_address == None:
            listen_address = Configuration.evaluation_server[0]
        if listen_port == None:
            listen_port = Configuration.evaluation_server[1]

        self.jd = JobDispatcher(len(Configuration.workers))
        self.st = threading.Thread()

        self.contest = contest
        if self.contest.ranking_view == None:
            self.contest.ranking_view = RankingView(self.contest)
            self.contest.to_couch()
        for submission in self.contest.submissions:
            if submission.evaluation_outcome == None:
                if submission.compilation_outcome != "fail":
                    self.add_job(submission.couch_id)
            else:
                scorer = submission.task.scorer
                scorer.add_submission(submission)
        self.contest.update_ranking_view()


        RPCServer.__init__(self, "EvaluationServer",
                           listen_address, listen_port,
                           [self.use_token,
                            self.add_job,
                            self.compilation_finished,
                            self.evaluation_finished,
                            self.self_destruct,
                            self.get_workers_status,
                            self.add_worker,
                            self.del_worker,
                            self.enable_worker],
                           thread=self.st,
                           start_now=False,
                           allow_none=True)

    def start(self):
        self.jd.start()
        self.st.start()

    def use_token(self, submission_id):
        """Called by CWS when the user wants to use a token.

        If the evaluation of the submission is already in the queue,
        we increase its priority; otherwise, we do nothing, since the
        priority will be set as medium when we queue the evaluation.

        Instead, if we already evaluated the submission, we signal to
        the scorer. This is used by scorers that use both the last
        submission and the best tokenized submission to build the
        score.
        """
        submission = CouchObject.from_couch(submission_id)
        if submission.evaluation_outcome != None:
            submission.task.scorer.add_token(submission)
        self.jd.queue_set_priority((EvaluationServer.JOB_TYPE_EVALUATION,
                                    submission),
                                   EvaluationServer.JOB_PRIORITY_MEDIUM)
        return True

    def add_job(self, submission_id):
        self.contest.refresh()
        Utils.log("Queueing compilation for submission %s" % submission_id)
        submission = CouchObject.from_couch(submission_id)
        self.jd.queue_push((EvaluationServer.JOB_TYPE_COMPILATION, submission),
                           EvaluationServer.JOB_PRIORITY_HIGH)
        return True

    def action_finished(self, job):
        worker = self.jd.find_worker(job)
        time_elapsed = time.time() - self.jd.workers.start_time[worker]
        Utils.log(("Worker %d (%s:%d) finished to %s submission %s " +
                   "(took around %.2f seconds)") %
                  (worker,
                   self.jd.workers.address[worker][0],
                   self.jd.workers.address[worker][1],
                   job[0],
                   job[1].couch_id,
                   time_elapsed))
        return self.jd.release_worker(worker)

    def compilation_finished(self, success, submission_id):
        """RPC method called by a Worker when a compilation has been
        completed.
        """
        submission = CouchObject.from_couch(submission_id)
        retry = True
        while retry:
            retry = False
            submission.compilation_tentatives += 1
            try:
                submission.to_couch()
            except couchdb.ResourceConflict:
                retry = True
                submission.refresh()
        self.action_finished((EvaluationServer.JOB_TYPE_COMPILATION,
                              submission))
        if success and submission.compilation_outcome == "ok":
            Utils.log(("Compilation succeeded for submission %s, " +
                       " queueing evaluation") % submission_id)
            priority = EvaluationServer.JOB_PRIORITY_LOW
            if submission.tokened():
                priority = EvaluationServer.JOB_PRIORITY_MEDIUM
            self.jd.queue_push((EvaluationServer.JOB_TYPE_EVALUATION,
                                submission),
                               priority)
        elif success and submission.compilation_outcome == "fail":
            Utils.log(("Compilation finished for submission %s, " +
                       "but the submission was not accepted; " +
                       "I'm not queueing evaluation") % submission_id)
        else:
            Utils.log("Compilation failed for submission %s" % submission_id)
            if submission.compilation_tentatives > \
                    EvaluationServer.MAX_COMPILATION_TENTATIVES:
                Utils.log(("Maximum tentatives (%d) reached for " +
                           "the compilation of submission %s - " +
                           "I will not try again") %
                          (EvaluationServer.MAX_COMPILATION_TENTATIVES,
                           submission_id), Utils.Logger.SEVERITY_IMPORTANT)
            else:
                self.jd.queue_push((EvaluationServer.JOB_TYPE_COMPILATION,
                                    submission),
                                   EvaluationServer.JOB_PRIORITY_HIGH)
        return True

    def evaluation_finished(self, success, submission_id):
        """RPC method called by a Worker when an evaluation has been
        completed.
        """
        submission = CouchObject.from_couch(submission_id)
        retry = True
        while retry:
            retry = False
            submission.evaluation_tentatives += 1
            try:
                submission.to_couch()
            except couchdb.ResourceConflict:
                retry = True
                submission.refresh()
        priority = self.action_finished((
                EvaluationServer.JOB_TYPE_EVALUATION, submission))[0]
        if success:
            Utils.log("Evaluation succeeded for submission %s" % submission_id)
            self.update_evaluation(submission_id)
        else:
            Utils.log("Evaluation failed for submission %s" % submission_id)
            if submission.evaluation_tentatives > \
                    EvaluationServer.MAX_EVALUATION_TENTATIVES:
                Utils.log(("Maximum tentatives (%d) reached for " +
                           "the evaluation of submission %s - " +
                           "I will not try again") %
                          (EvaluationServer.MAX_EVALUATION_TENTATIVES,
                           submission_id), Utils.Logger.SEVERITY_IMPORTANT)
            else:
                self.jd.queue_push((EvaluationServer.JOB_TYPE_EVALUATION,
                                    submission),
                                   priority)
        return True

    def update_evaluation(self, submission_id):
        """Computes the evaluation for all submissions of the same
        task as submission_id's task, assuming that only submission_id
        has changed from last evaluation.
        """
        submission = CouchObject.from_couch(submission_id)
        scorer = submission.task.scorer
        scorer.add_submission(submission)
        self.contest.update_ranking_view()

    def self_destruct(self):
        self.jd.queue_push((EvaluationServer.JOB_TYPE_BOMB, None),
                           EvaluationServer.JOB_PRIORITY_EXTRA_HIGH)
        return True

    def get_workers_status(self):
        return self.jd.get_workers_status()

    def enable_worker(self, n):
        self.jd.enable_worker(n)

    def add_worker(self, n, addr, port):
        self.jd.add_worker(n, addr, port)

    def del_worker(self, n):
        self.jd.del_worker(n)


def sigterm(signum, stack):
    global e
    print "Trying to self destruct"
    e.self_destruct()


if __name__ == "__main__":
    import sys

    es_address, es_port = Configuration.evaluation_server

    if sys.argv[1] == "run":
        Utils.set_service("evaluation server")
        c = Utils.ask_for_contest(skip=1)
        e = EvaluationServer(c, es_address, es_port)
        e.start()
        signal.signal(signal.SIGTERM, sigterm)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            e.self_destruct()

    elif sys.argv[1] == "destroy":
        es = xmlrpclib.ServerProxy("http://%s:%d" % (es_address, es_port))
        es.self_destruct()

    elif sys.argv[1] == "submit":
        import Submission
        c = Utils.ask_for_contest(skip=1)
        t = c.tasks[0]
        if len(sys.argv) >= 5:
            t = CouchObject.from_couch(sys.argv[4])
        s = Submission.sample_submission(user=c.users[0],
                                         task=t,
                                         files=[sys.argv[3]])
        c.submissions.append(s)
        c.to_couch()
        es = xmlrpclib.ServerProxy("http://%s:%d" % (es_address, es_port))
        es.add_job(s.couch_id)
        print "Submission %s" % s.couch_id

    elif sys.argv[1] == "token":
        # FIXME - This piece of code is a copy from its equivalent in
        # ContestWebServer.py. This is bad: they should be merged in
        # some client library which is used by all those who want to
        # interact with the ES.
        c = Utils.ask_for_contest(skip=1)
        timestamp = time.time()
        ident = sys.argv[3]
        for s in c.submissions:
            if s.couch_id == ident:
                # If the user already used a token on this
                if s.tokened():
                    print "This submission is already marked for " + \
                        "for detailed feedback."
                else:
                    print "No tokens available."
                    break
        else:
            print "Submission not found in the specified contest"

    elif sys.argv[1] == "dump":
        obj = CouchObject.from_couch(sys.argv[2])
        print obj.dump()

    elif sys.argv[1] == "set":
        obj = CouchObject.from_couch(sys.argv[2])
        try:
            val = int(sys.argv[4])
        except ValueError:
            val = sys.argv[4]
        obj.__dict__[sys.argv[3]] = val
        obj.to_couch()
        print obj.dump()

    elif sys.argv[1] == "get_workers_status":
        es = xmlrpclib.ServerProxy("http://%s:%d" % (es_address, es_port))
        status = es.get_workers_status()
        for k in sorted(status.keys()):
            print "%5s: %s" % (k, str(status[k]))

    elif sys.argv[1] == "enable_worker":
        es = xmlrpclib.ServerProxy("http://%s:%d" % (es_address, es_port))
        es.enable_worker(int(sys.argv[2]))

    elif sys.argv[1] == "del_worker":
        es = xmlrpclib.ServerProxy("http://%s:%d" % (es_address, es_port))
        es.del_worker(int(sys.argv[2]))

    elif sys.argv[1] == "add_worker":
        es = xmlrpclib.ServerProxy("http://%s:%d" % (es_address, es_port))
        es.add_worker(int(sys.argv[2]), sys.argv[3], int(sys.argv[4]))

    elif sys.argv[1] == "exit_worker":
        wor = xmlrpclib.ServerProxy("http://%s:%d" % (sys.argv[2],
                                                      int(sys.argv[3])))
        wor.shut_down(sys.argv[4])
