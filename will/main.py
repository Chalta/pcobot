# -*- coding: utf-8 -*-

import datetime
import imp
import inspect
import logging
import operator
import os
import re
import signal
import sys
import threading
import time
import traceback
from importlib import import_module
from clint.textui import colored, puts, indent
from os.path import abspath, dirname
from multiprocessing import Process, Queue
from multiprocessing.queues import Empty

import bottle

from listener import ListenerMixin

from mixins import ScheduleMixin, StorageMixin, ErrorMixin,\
    RoomMixin, PluginModulesLibraryMixin, EmailMixin, PubSubMixin
from backends import analysis, execution, generation, io_adapters
from backends.io_adapters.base import Event
from scheduler import Scheduler
import settings
from utils import show_valid, error, warn, print_head, Bunch


# Force UTF8
if sys.version_info < (3, 0):
    reload(sys)
    sys.setdefaultencoding('utf8')
else:
    raw_input = input


# Update path
PROJECT_ROOT = abspath(os.path.join(dirname(__file__)))
PLUGINS_ROOT = abspath(os.path.join(PROJECT_ROOT, "plugins"))
TEMPLATES_ROOT = abspath(os.path.join(PROJECT_ROOT, "templates"))
PROJECT_TEMPLATE_ROOT = abspath(os.path.join(os.getcwd(), "templates"))
sys.path.append(PROJECT_ROOT)
sys.path.append(os.path.join(PROJECT_ROOT, "will"))


class WillBot(EmailMixin, StorageMixin, ScheduleMixin, PubSubMixin,
              ErrorMixin, RoomMixin, ListenerMixin, PluginModulesLibraryMixin):

    def __init__(self, **kwargs):
        if "template_dirs" in kwargs:
            warn("template_dirs is now depreciated")
        if "plugin_dirs" in kwargs:
            warn("plugin_dirs is now depreciated")

        log_level = getattr(settings, 'LOGLEVEL', logging.ERROR)
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%a, %d %b %Y %H:%M:%S',
        )

        # Find all the PLUGINS modules
        plugins = settings.PLUGINS
        self.plugins_dirs = {}

        # Set template dirs.
        full_path_template_dirs = []
        for t in settings.TEMPLATE_DIRS:
            full_path_template_dirs.append(os.path.abspath(t))

        # Add will's templates_root
        if TEMPLATES_ROOT not in full_path_template_dirs:
            full_path_template_dirs += [TEMPLATES_ROOT, ]

        # Add this project's templates_root
        if PROJECT_TEMPLATE_ROOT not in full_path_template_dirs:
            full_path_template_dirs += [PROJECT_TEMPLATE_ROOT, ]

        # Convert those to dirs
        for plugin in plugins:
            path_name = None
            for mod in plugin.split('.'):
                if path_name is not None:
                    path_name = [path_name]
                file_name, path_name, description = imp.find_module(mod, path_name)

            # Add, uniquely.
            self.plugins_dirs[os.path.abspath(path_name)] = plugin

            if os.path.exists(os.path.join(os.path.abspath(path_name), "templates")):
                full_path_template_dirs.append(
                    os.path.join(os.path.abspath(path_name), "templates")
                )

        # Key by module name
        self.plugins_dirs = dict(zip(self.plugins_dirs.values(), self.plugins_dirs.keys()))

        # Storing here because storage hasn't been bootstrapped yet.
        os.environ["WILL_TEMPLATE_DIRS_PICKLED"] =\
            ";;".join(full_path_template_dirs)

    def bootstrap(self):
        print_head()
        self.load_config()
        self.verify_environment()
        self.verify_rooms()
        self.bootstrap_storage_mixin()
        self.bootstrap_pubsub_mixin()
        self.bootstrap_plugins()
        self.verify_plugin_settings()
        self.verify_io()
        self.verify_analysis()
        self.verify_generate()
        self.verify_execution()
        self.bootstrap_execution()

        puts("Bootstrapping complete.")
        puts("\nStarting core processes:")
        self.queues = Bunch()
        self.queues.io = Bunch()
        self.queues.io._incoming = Queue()
        self.queues.analysis = Bunch()
        self.queues.generation = Bunch()

        # self.queues.stdin = Queue()

        try:
            # Scheduler
            scheduler_thread = Process(target=self.bootstrap_scheduler)
            # scheduler_thread.daemon = True

            # Bottle
            bottle_thread = Process(target=self.bootstrap_bottle)
            # bottle_thread.daemon = True

            # Incoming message queue
            incoming_message_thread = Process(target=self.bootstrap_message_handler)

            # Event handler
            incoming_event_thread = Process(target=self.bootstrap_event_handler)

            self.io_threads = []
            self.analysis_threads = []
            self.generation_threads = []
            self.message_handler_threads = []

            with indent(2):
                # Start up threads.
                self.bootstrap_io()
                self.bootstrap_analysis()
                self.bootstrap_generation()

                scheduler_thread.start()
                bottle_thread.start()
                incoming_message_thread.start()
                incoming_event_thread.start()

                errors = self.get_startup_errors()
                if len(errors) > 0:
                    default_room = self.get_room_from_name_or_id(settings.DEFAULT_ROOM)["room_id"]
                    error_message = "FYI, I ran into some problems while starting up:"
                    for err in errors:
                        error_message += "\n%s\n" % err
                    self.send_room_message(default_room, error_message, color="yellow")
                    puts(colored.red(error_message))

                signal.signal(signal.SIGINT, self.handle_sys_exit)
                # TODO this hangs for some reason.
                # signal.signal(signal.SIGTERM, self.handle_sys_exit)

                self.stdin_listener_thread = False
                if self.has_stdin_io_backend:
                    self.queues.io.stdin_input = Queue()
                    self.stdin_listener_thread = Process(target=self.handle_main_stdin_queue)
                    self.stdin_listener_thread.start()

                    self.current_line = ""
                    while True:
                        for line in sys.stdin.readline():
                            if "\n" in line:
                                self.queues.io.stdin_input.put(self.current_line)
                                self.current_line = ""
                            else:
                                self.current_line += line
                while True:
                    time.sleep(100)
        except (KeyboardInterrupt, SystemExit):
            scheduler_thread.terminate()
            bottle_thread.terminate()
            incoming_message_thread.terminate()
            incoming_event_thread.terminate()

            if self.stdin_listener_thread:
                self.stdin_listener_thread.terminate()

            # for t in self.stdin_io_threads:
            #     t.terminate()

            # for t in self.io_threads:
            #     t.terminate()

            for q in self.queues.io.terminate:
                q.put({"type": "terminate"})

            for t in self.analysis_threads:
                t.terminate()

            for t in self.generation_threads:
                t.terminate()

            for t in self.message_handler_threads:
                t.terminate()

            print '\n\nReceived keyboard interrupt, quitting threads.',
            while (
                scheduler_thread.is_alive() or
                bottle_thread.is_alive() or
                incoming_message_thread.is_alive() or
                incoming_event_thread.is_alive() or
                self.stdin_listener_thread.is_alive() or
                any([t.is_alive() for t in self.stdin_io_threads]) or
                any([t.is_alive() for t in self.stdin_io_threads]) or
                any([t.is_alive() for t in self.analysis_threads]) or
                any([t.is_alive() for t in self.generation_threads]) or
                any([t.is_alive() for t in self.message_handler_threads])
                # or
                # ("hipchat" in settings.CHAT_BACKENDS and xmpp_thread and xmpp_thread.is_alive())
            ):
                    sys.stdout.write(".")
                    sys.stdout.flush()
                    time.sleep(0.5)
            print ". done.\n"

    def verify_individual_setting(self, test_setting, quiet=False):
        if not test_setting.get("only_if", True):
            return True

        if hasattr(settings, test_setting["name"][5:]):
            with indent(2):
                show_valid(test_setting["name"])
            return True
        else:
            error("%(name)s... missing!" % test_setting)
            with indent(2):
                puts("""To obtain a %(name)s: \n%(obtain_at)s

To set your %(name)s:
1. On your local machine, add this to your virtual environment's bin/postactivate file:
   export %(name)s=YOUR_ACTUAL_%(name)s
2. If you've deployed will on heroku, run
   heroku config:set %(name)s=YOUR_ACTUAL_%(name)s
""" % test_setting)
            return False

    def verify_environment(self):
        missing_settings = False
        required_settings = []
        if "hipchat" in settings.CHAT_BACKENDS:
            required_settings = [
                {
                    "name": "WILL_USERNAME",
                    "obtain_at": """1. Go to hipchat, and create a new user for will.
    2. Log into will, and go to Account settings>XMPP/Jabber Info.
    3. On that page, the 'Jabber ID' is the value you want to use.""",
                },
                {
                    "name": "WILL_PASSWORD",
                    "obtain_at": (
                        "1. Go to hipchat, and create a new user for will.  "
                        "Note that password - this is the value you want. "
                        "It's used for signing in via XMPP."
                    ),
                },
                {
                    "name": "WILL_V2_TOKEN",
                    "obtain_at": """1. Log into hipchat using will's user.
    2. Go to https://your-org.hipchat.com/account/api
    3. Create a token.
    4. Copy the value - this is the WILL_V2_TOKEN.""",
                },
                {
                    "name": "WILL_REDIS_URL",
                    "only_if": getattr(settings, "STORAGE_BACKEND", "redis") == "redis",
                    "obtain_at": """1. Set up an accessible redis host locally or in production
    2. Set WILL_REDIS_URL to its full value, i.e. redis://localhost:6379/7""",
                },
            ]

        puts("")
        puts("Verifying environment...")

        for r in required_settings:
            if not self.verify_individual_setting(r):
                missing_settings = True

        if missing_settings:
            error(
                "Will was unable to start because some required environment "
                "variables are missing.  Please fix them and try again!"
            )
            sys.exit(1)
        else:
            puts("")

        if "hipchat" in settings.CHAT_BACKENDS:

            puts("Verifying credentials...")
            # Parse 11111_222222@chat.hipchat.com into id, where 222222 is the id.
            user_id = settings.USERNAME.split('@')[0].split('_')[1]

            # Splitting into a thread. Necessary because *BSDs (including OSX) don't have threadsafe DNS.
            # http://stackoverflow.com/questions/1212716/python-interpreter-blocks-multithreaded-dns-requests
            q = Queue()
            p = Process(target=self.get_user, args=(user_id,), kwargs={"q": q, })
            p.start()
            user_data = q.get()
            p.join()

            if "error" in user_data:
                error("We ran into trouble: '%(message)s'" % user_data["error"])
                sys.exit(1)
            with indent(2):
                show_valid("%s authenticated" % user_data["name"])
                os.environ["WILL_NAME"] = user_data["name"]
                show_valid("@%s verified as handle" % user_data["mention_name"])
                os.environ["WILL_HANDLE"] = user_data["mention_name"]

            puts("")

    def load_config(self):
        puts("Loading configuration...")
        with indent(2):
            settings.import_settings(quiet=False)
        puts("")

    def verify_rooms(self):
        if "hipchat" in settings.CHAT_BACKENDS:
            puts("Verifying rooms...")
            # If we're missing ROOMS, join all of them.
            with indent(2):
                if settings.ROOMS is None:
                    # Yup. Thanks, BSDs.
                    q = Queue()
                    p = Process(target=self.update_available_rooms, args=(), kwargs={"q": q, })
                    p.start()
                    rooms_list = q.get()
                    show_valid("Joining all %s known rooms." % len(rooms_list))
                    os.environ["WILL_ROOMS"] = ";".join(rooms_list)
                    p.join()
                    settings.import_settings()
                else:
                    show_valid(
                        "Joining the %s room%s specified." % (
                            len(settings.ROOMS),
                            "s" if len(settings.ROOMS) > 1 else ""
                        )
                    )
            puts("")

    def verify_io(self):
        puts("Verifying IO backends...")
        missing_settings = False
        missing_setting_error_messages = []
        one_valid_backend = False

        if not hasattr(settings, "IO_BACKENDS"):
            settings.IO_BACKENDS = ["will.backends.io_adapters.shell", ]
        # Try to import them all, catch errors and output trouble if we hit it.
        for b in settings.IO_BACKENDS:
            with indent(2):
                try:
                    path_name = None
                    for mod in b.split('.'):
                        if path_name is not None:
                            path_name = [path_name]
                        file_name, path_name, description = imp.find_module(mod, path_name)

                    one_valid_backend = True
                    show_valid("%s" % b)
                except ImportError, e:
                    error_message = (
                        "IO backend %s is missing. Please either remove it \nfrom config.py "
                        "or WILL_IO_BACKENDS, or provide it somehow (pip install, etc)."
                    ) % b
                    puts(colored.red("✗ %s" % b))
                    puts()
                    puts(error_message)
                    puts()
                    puts(traceback.format_exc(e))
                    missing_setting_error_messages.append(error_message)
                    missing_settings = True

        if missing_settings and not one_valid_backend:
            puts("")
            error(
                "Unable to find a valid IO backend - will has no way to talk "
                "or listen!\n       Quitting now, please look at the above errors!\n"
            )
            sys.exit(1)
        puts()

    def verify_analysis(self):
        puts("Verifying Analysis backends...")
        missing_settings = False
        missing_setting_error_messages = []
        one_valid_backend = False

        if not hasattr(settings, "ANALYZE_BACKENDS"):
            settings.ANALYZE_BACKENDS = ["will.backends.analysis.nothing", ]
        # Try to import them all, catch errors and output trouble if we hit it.
        for b in settings.ANALYZE_BACKENDS:
            with indent(2):
                try:
                    path_name = None
                    for mod in b.split('.'):
                        if path_name is not None:
                            path_name = [path_name]
                        file_name, path_name, description = imp.find_module(mod, path_name)

                    one_valid_backend = True
                    show_valid("%s" % b)
                except ImportError, e:
                    error_message = (
                        "Analysis backend %s is missing. Please either remove it \nfrom config.py "
                        "or WILL_ANALYZE_BACKENDS, or provide it somehow (pip install, etc)."
                    ) % b
                    puts(colored.red("✗ %s" % b))
                    puts()
                    puts(error_message)
                    puts()
                    puts(traceback.format_exc(e))
                    missing_setting_error_messages.append(error_message)
                    missing_settings = True

        if missing_settings and not one_valid_backend:
            puts("")
            error(
                "Unable to find a valid IO backend - will has no way to talk "
                "or listen!\n       Quitting now, please look at the above errors!\n"
            )
            sys.exit(1)
        puts()

    def verify_generate(self):
        puts("Verifying Generation backends...")
        missing_settings = False
        missing_setting_error_messages = []
        one_valid_backend = False

        if not hasattr(settings, "GENERATION_BACKENDS"):
            settings.GENERATION_BACKENDS = ["will.backends.generation.regex", ]
        # Try to import them all, catch errors and output trouble if we hit it.
        for b in settings.GENERATION_BACKENDS:
            with indent(2):
                try:
                    path_name = None
                    for mod in b.split('.'):
                        if path_name is not None:
                            path_name = [path_name]
                        file_name, path_name, description = imp.find_module(mod, path_name)

                    one_valid_backend = True
                    show_valid("%s" % b)
                except ImportError, e:
                    error_message = (
                        "Generation backend %s is missing. Please either remove it \nfrom config.py "
                        "or WILL_GENERATION_BACKENDS, or provide it somehow (pip install, etc)."
                    ) % b
                    puts(colored.red("✗ %s" % b))
                    puts()
                    puts(error_message)
                    puts()
                    puts(traceback.format_exc(e))
                    missing_setting_error_messages.append(error_message)
                    missing_settings = True

        if missing_settings and not one_valid_backend:
            puts("")
            error(
                "Unable to find a valid IO backend - will has no way to talk "
                "or listen!\n       Quitting now, please look at the above errors!\n"
            )
            sys.exit(1)
        puts()

    def verify_execution(self):
        puts("Verifying Execution backend...")
        missing_settings = False
        missing_setting_error_messages = []
        one_valid_backend = False

        if not hasattr(settings, "EXECUTION_BACKENDS"):
            settings.EXECUTION_BACKENDS = ["will.backends.execution.all", ]

        with indent(2):
            for b in settings.EXECUTION_BACKENDS:
                try:
                    path_name = None
                    for mod in b.split('.'):
                        if path_name is not None:
                            path_name = [path_name]
                        file_name, path_name, description = imp.find_module(mod, path_name)

                    one_valid_backend = True
                    show_valid("%s" % b)
                except ImportError, e:
                    error_message = (
                        "Execution backend %s is missing. Please either remove it \nfrom config.py "
                        "or WILL_EXECUTION_BACKENDS, or provide it somehow (pip install, etc)."
                    ) % b
                    puts(colored.red("✗ %s" % b))
                    puts()
                    puts(error_message)
                    puts()
                    puts(traceback.format_exc(e))
                    missing_setting_error_messages.append(error_message)
                    missing_settings = True

        if missing_settings and not one_valid_backend:
            puts("")
            error(
                "Unable to find a valid IO backend - will has no way to talk "
                "or listen!\n       Quitting now, please look at the above errors!\n"
            )
            sys.exit(1)
        puts()

    def bootstrap_execution(self):
        missing_setting_error_messages = []
        self.execution_backends = []
        execution_backends = getattr(settings, "EXECUTION_BACKENDS", ["will.backends.execution.all", ])
        for b in execution_backends:
            module = import_module(b)
            for class_name, cls in inspect.getmembers(module, predicate=inspect.isclass):
                try:
                    if (
                        hasattr(cls, "is_will_execution_backend") and
                        cls.is_will_execution_backend and
                        class_name != "ExecutionBackend"
                    ):
                        c = cls(bot=self)
                        self.execution_backends.append(c)
                except ImportError, e:
                    error_message = (
                        "Execution backend %s is missing. Please either remove it \nfrom config.py "
                        "or WILL_EXECUTION_BACKENDS, or provide it somehow (pip install, etc)."
                    ) % settings.EXECUTION_BACKENDS
                    puts(colored.red("✗ %s" % settings.EXECUTION_BACKENDS))
                    puts()
                    puts(error_message)
                    puts()
                    puts(traceback.format_exc(e))
                    missing_setting_error_messages.append(error_message)

        if len(self.execution_backends) == 0:
            puts("")
            error(
                "Unable to find a valid execution backend - will has no way to make decisions!"
                "\n       Quitting now, please look at the above error!\n"
            )
            sys.exit(1)

    def verify_plugin_settings(self):
        puts("Verifying settings requested by plugins...")

        missing_settings = False
        missing_setting_error_messages = []
        with indent(2):
            for name, meta in self.required_settings_from_plugins.items():
                if not hasattr(settings, name):
                    error_message = (
                        "%(setting_name)s is missing. It's required by the"
                        "%(plugin_name)s plugin's '%(function_name)s' method."
                    ) % meta
                    puts(colored.red("✗ %(setting_name)s" % meta))
                    missing_setting_error_messages.append(error_message)
                    missing_settings = True
                else:
                    show_valid("%(setting_name)s" % meta)

            if missing_settings:
                puts("")
                warn(
                    "Will is missing settings required by some plugins. "
                    "He's starting up anyway, but you will run into errors"
                    " if you try to use those plugins!"
                )
                self.add_startup_error("\n".join(missing_setting_error_messages))
            else:
                puts("")

    def handle_sys_exit(self, *args, **kwargs):
        sys.exit(1)

    def handle_main_stdin_queue(self):
        while True:
            try:
                line = self.queues.io.stdin_input.get(timeout=settings.QUEUE_INTERVAL)
                for name, q in self.queues.io.stdin_backend_queues.items():
                    q.put(Event(
                        type="message",
                        source="stdin",
                        content=line,
                    ))
            except:
                # import traceback; traceback.print_exc();
                pass

    def bootstrap_message_handler(self):
        self.analysis_timeout = getattr(settings, "ANALYSIS_TIMEOUT_MS", 2000)
        self.generation_timeout = getattr(settings, "GENERATION_TIMEOUT_MS", 2000)
        self.message_handler_threads = []

        while True:
            try:
                message = self.queues.io._incoming.get(timeout=settings.QUEUE_INTERVAL)
                t = Process(target=self.handle_message, args=(message,))
                t.start()
                self.message_handler_threads.append(t)
            except Empty:
                pass

    def bootstrap_event_handler(self):
        self.analysis_timeout = getattr(settings, "ANALYSIS_TIMEOUT_MS", 2000)
        self.generation_timeout = getattr(settings, "GENERATION_TIMEOUT_MS", 2000)
        self.message_handler_threads = []
        self.pubsub.subscribe(["message.incoming", "analysis.*", "generation.*"])

        # TODO: change this to the number of running analysis threads
        num_analysis_queues = len(self.queues.analysis.input)
        num_generation_queues = len(self.queues.generation.input)
        analysis_queues = {}
        generation_queues = {}
        # execution_queues = {}

        while True:
            try:
                event = self.pubsub.get_message()
                if event:
                    # and hasattr(event, "type"):
                    print "--- MAIN got event (%s)" % event.type
                    print event
                    # print event.__dict__
                    if hasattr(event, "source"):
                        print "event.data.source.__dict__"
                        print event.data.source.__dict__
                    # print "type" in event
                    print type(event)
                    if hasattr(event, "type"):
                        print "event.type"
                        print event.type
                        print event.type == "message.incoming"

                        # TOOD: Order by most common.
                        if event.type == "message.incoming":
                            # A message just got dropped off one of the IO Backends.
                            # Send it to analysis.

                            # elif event.type == "analysis.start":
                            analysis_queues[event.source_hash] = {
                                "count": 0,
                                "timeout_end": datetime.datetime.now() + datetime.timedelta(seconds=self.analysis_timeout/1000),
                                "source": event,
                            }
                            self.pubsub.publish("analysis.start", event.data.source, reference_message=event)

                        elif event.type == "analysis.complete":
                            print analysis_queues
                            q = analysis_queues[event.source_hash]
                            q["source"].update({"analysis": event.data})
                            q["count"] += 1
                            print q["count"]
                            print num_analysis_queues
                            print q["count"] >= num_analysis_queues
                            print datetime.datetime.now() > q["timeout_end"]

                            if q["count"] >= num_analysis_queues or datetime.datetime.now() > q["timeout_end"]:
                                # done, move on.
                                generation_queues[event.source_hash] = {
                                    "count": 0,
                                    "timeout_end": datetime.datetime.now() + datetime.timedelta(seconds=self.generation_timeout/1000),
                                    "source": q["source"],
                                }
                                print "generation_queues"
                                print generation_queues
                                del analysis_queues[event.source_hash]
                                self.pubsub.publish("generation.start", q["source"], reference_message=q["source"])

                        elif event.type == "generation.complete":
                            q = generation_queues[event.source_hash]
                            print "TODO: FIx this properly."
                            if not hasattr(q["source"], "generation_options"):
                                q["source"].generation_options = []
                            if hasattr(event, "data") and len(event.data) > 0:
                                q["source"].generation_options.append(*event.data)
                            q["count"] += 1
                            print q["count"]
                            print num_generation_queues

                            if q["count"] >= num_generation_queues or datetime.datetime.now() > q["timeout_end"]:
                                # done, move on.
                                # self.pubsub.publish("execution.start", q["source"])
                                print self.execution_backends
                                for b in self.execution_backends:
                                    try:
                                        b.execute(q["source"])
                                    except:
                                        break
                                del generation_queues[event.source_hash]
                        time.sleep(settings.QUEUE_INTERVAL)
            except:
                logging.exception("Error handling message")

            # try:
            #     message = self.queues.io._incoming.get(timeout=settings.QUEUE_INTERVAL)
            #     t = Process(target=self.handle_message, args=(message,))
            #     t.start()
            #     self.message_handler_threads.append(t)
            # except Empty:
            #     pass

    def handle_message(self, message):

        # Send to analyze
        # Analysis (Understand, add metadata)
        # Now: Run each analyze syncrhonously, add info to message.
        # Later: Fire up analyze threads for each, with queues for output
        message_id = message.hash
        self.pubsub.subscribe("analysis")
        return

        try:
            message.analysis = Bunch()

            queues_heard_from = 0
            timeout_end = datetime.datetime.now() + datetime.timedelta(seconds=self.analysis_timeout/1000)
            self.pubsub.publish("analysis.start", message)
            # for name, q in self.queues.analysis.input.items():
            #     q.put(message)

            # Grab results from all the queues, give up on timeout after
            while queues_heard_from < num_queues and datetime.datetime.now() < timeout_end:
                m = self.pubsub.get_message()
                if (
                    m and
                    hasattr(m, "type") and
                    m.type == "analysis.complete" and
                    m.hash == message_id
                ):
                    # TODO: Last one wins right now - this should not
                    #         # allow conflicts.
                    message.analysis.update(m)
                    queues_heard_from += 1
                else:
                    time.sleep(settings.QUEUE_INTERVAL)

                # for name, q in self.queues.analysis.output.items():
                #     try:
                #         context = q.get(timeout=settings.QUEUE_INTERVAL)
                #         # TODO: Last one wins right now - this should not
                #         # allow conflicts.
                #         message.analysis.update(context)

                #         queues_heard_from += 1
                #     except Empty:
                #         pass

            if datetime.datetime.now() > timeout_end:
                print "timed out"

            # Generate (make a list of possible responses)
            message.generation_options = []

            num_queues = len(self.queues.generation.input)
            queues_heard_from = 0
            timeout_end = datetime.datetime.now() + datetime.timedelta(seconds=self.generation_timeout/1000)
            for name, q in self.queues.generation.input.items():
                q.put(message)

            # Grab results from all the queues, give up on timeout after
            while queues_heard_from < num_queues and datetime.datetime.now() < timeout_end:
                for name, q in self.queues.generation.output.items():
                    try:
                        options = q.get(timeout=settings.QUEUE_INTERVAL)
                        # TODO: Last one wins right now - this should not
                        # allow conflicts.
                        for o in options:
                            message.generation_options.append(o)

                        queues_heard_from += 1
                    except Empty:
                        pass
            if datetime.datetime.now() > timeout_end:
                print "timed out"

            # print "generation done."

            # print "ready for execution"
            # Execute (choose from the possible responses based on the highest score.)
            # Possibly do some analysis to post-check and re-decide an outcome.
            # Take an action.
            for b in self.execution_backends:
                try:
                    b.execute(message)
                except:
                    break
        except:
            logging.critical(
                "Error running %s.  \n\n%s\nContinuing...\n" % (
                    message,
                    traceback.format_exc()
                )
            )

    def bootstrap_storage_mixin(self):
        puts("Bootstrapping storage...")
        try:
            self.bootstrap_storage()
            with indent(2):
                show_valid("Bootstrapped!")
            puts("")
        except ImportError, e:
            module_name = traceback.format_exc(e).split(" ")[-1]
            error("Unable to bootstrap storage - attempting to load %s" % module_name)
            puts(traceback.format_exc(e))
            sys.exit(1)
        except Exception, e:
            error("Unable to bootstrap storage!")
            puts(traceback.format_exc(e))
            sys.exit(1)

    def bootstrap_pubsub_mixin(self):
        puts("Bootstrapping storage...")
        try:
            self.bootstrap_pubsub()
            with indent(2):
                show_valid("Bootstrapped!")
            puts("")
        except ImportError, e:
            module_name = traceback.format_exc(e).split(" ")[-1]
            error("Unable to bootstrap pubsub - attempting to load %s" % module_name)
            puts(traceback.format_exc(e))
            sys.exit(1)
        except Exception, e:
            error("Unable to bootstrap pubsub!")
            puts(traceback.format_exc(e))
            sys.exit(1)

    def bootstrap_scheduler(self):
        bootstrapped = False
        try:
            self.save("plugin_modules_library", self._plugin_modules_library)
            Scheduler.clear_locks(self)
            self.scheduler = Scheduler()

            for plugin_info, fn, function_name in self.periodic_tasks:
                meta = fn.will_fn_metadata
                self.add_periodic_task(
                    plugin_info["full_module_name"],
                    plugin_info["name"],
                    function_name,
                    meta["sched_args"],
                    meta["sched_kwargs"],
                    meta["function_name"],
                )
            for plugin_info, fn, function_name in self.random_tasks:
                meta = fn.will_fn_metadata
                self.add_random_tasks(
                    plugin_info["full_module_name"],
                    plugin_info["name"],
                    function_name,
                    meta["start_hour"],
                    meta["end_hour"],
                    meta["day_of_week"],
                    meta["num_times_per_day"]
                )
            bootstrapped = True
        except Exception, e:
            self.startup_error("Error bootstrapping scheduler", e)
        if bootstrapped:
            show_valid("Scheduler started.")
            self.scheduler.start_loop(self)

    def bootstrap_bottle(self):
        bootstrapped = False
        try:
            for cls, function_name in self.bottle_routes:
                instantiated_cls = cls(bot=self)
                instantiated_fn = getattr(instantiated_cls, function_name)
                bottle_route_args = {}
                for k, v in instantiated_fn.will_fn_metadata.items():
                    if "bottle_" in k and k != "bottle_route":
                        bottle_route_args[k[len("bottle_"):]] = v
                bottle.route(instantiated_fn.will_fn_metadata["bottle_route"], **bottle_route_args)(instantiated_fn)
            bootstrapped = True
        except Exception, e:
            self.startup_error("Error bootstrapping bottle", e)
        if bootstrapped:
            show_valid("Web server started.")
            bottle.run(host='0.0.0.0', port=settings.HTTPSERVER_PORT, server='cherrypy', quiet=True)

    def bootstrap_io(self):
        # puts("Bootstrapping IO...")
        self.has_stdin_io_backend = False
        self.io_backends = []
        self.queues.io.stdin_queue = []
        self.queues.io.stdin_backend_queues = {}
        self.queues.io.terminate = []
        self.queues.io.output = {}
        self.stdin_io_threads = []
        self.stdin_io_backends = []
        for b in settings.IO_BACKENDS:
            module = import_module(b)
            for class_name, cls in inspect.getmembers(module, predicate=inspect.isclass):
                try:
                    if (
                        hasattr(cls, "is_will_iobackend") and
                        cls.is_will_iobackend and
                        class_name != "IOBackend" and
                        class_name != "StdInOutIOBackend"
                    ):
                        c = cls()
                        my_output_queue = Queue()
                        self.queues.io.output[b] = my_output_queue
                        my_stdin_queue = Queue()
                        my_terminate_queue = Queue()
                        self.queues.io.terminate.append(my_terminate_queue)

                        if hasattr(c, "stdin_process") and c.stdin_process:
                            self.queues.io.stdin_backend_queues[b] = my_stdin_queue
                            thread = Process(
                                target=c._start,
                                args=(
                                    b,
                                    self.queues.io._incoming,
                                    self.queues.io.output[b],
                                    my_terminate_queue,
                                ),
                                kwargs={
                                    "stdin_queue": self.queues.io.stdin_backend_queues[b],
                                },
                            )
                            thread.start()
                            self.has_stdin_io_backend = True
                            self.stdin_io_threads.append(thread)
                        else:
                            thread = Process(
                                target=c._start,
                                args=(
                                    b,
                                    self.queues.io._incoming,
                                    self.queues.io.output[b],
                                    my_terminate_queue
                                )
                            )
                            thread.start()
                            self.io_threads.append(thread)

                        show_valid("%s Backend started." % cls.friendly_name)
                except Exception, e:
                    self.startup_error("Error bootstrapping %s io" % b, e)

            self.io_backends.append(b)

    def bootstrap_analysis(self):

        self.analysis_backends = []
        self.analysis_threads = []
        self.queues.analysis.input = {}
        self.queues.analysis.output = {}

        for b in settings.ANALYZE_BACKENDS:
            module = import_module(b)
            for class_name, cls in inspect.getmembers(module, predicate=inspect.isclass):
                try:
                    if (
                        hasattr(cls, "is_will_analysisbackend") and
                        cls.is_will_analysisbackend and
                        class_name != "AnalysisBackend"
                    ):
                        c = cls()
                        my_analysis_input_queue = Queue()
                        my_analysis_output_queue = Queue()
                        self.queues.analysis.input[b] = my_analysis_input_queue
                        self.queues.analysis.output[b] = my_analysis_output_queue
                        thread = Process(
                            target=c.start,
                            args=(
                                b,
                                self.queues.analysis.input[b],
                                self.queues.analysis.output[b],
                            ),
                            kwargs={"bot": self},
                        )
                        thread.start()
                        self.analysis_threads.append(thread)
                        show_valid("%s Backend started." % cls.__name__)
                except Exception, e:
                    self.startup_error("Error bootstrapping %s io" % b, e)

            self.analysis_backends.append(b)
        pass

    def bootstrap_generation(self):
        self.generation_backends = []
        self.generation_threads = []
        self.queues.generation.input = {}
        self.queues.generation.output = {}

        for b in settings.GENERATION_BACKENDS:
            module = import_module(b)
            for class_name, cls in inspect.getmembers(module, predicate=inspect.isclass):
                try:
                    if (
                        hasattr(cls, "is_will_generationbackend") and
                        cls.is_will_generationbackend and
                        class_name != "GenerationBackend"
                    ):
                        c = cls()
                        my_generation_input_queue = Queue()
                        my_generation_output_queue = Queue()
                        self.queues.generation.input[b] = my_generation_input_queue
                        self.queues.generation.output[b] = my_generation_output_queue
                        thread = Process(
                            target=c.start,
                            args=(
                                b,
                                self.queues.generation.input[b],
                                self.queues.generation.output[b],
                            ),
                            kwargs={"bot": self},
                        )
                        thread.start()
                        self.generation_threads.append(thread)
                        show_valid("%s Backend started." % cls.__name__)
                except Exception, e:
                    self.startup_error("Error bootstrapping %s io" % b, e)

            self.generation_backends.append(b)
        pass

    def bootstrap_plugins(self):
        puts("Bootstrapping plugins...")
        OTHER_HELP_HEADING = "Other"
        plugin_modules = {}
        plugin_modules_library = {}

        # NOTE: You can't access self.storage here, or it will deadlock when the threads try to access redis.
        with indent(2):
            parent_help_text = None
            for plugin_name, plugin_root in self.plugins_dirs.items():
                for root, dirs, files in os.walk(plugin_root, topdown=False):
                    for f in files:
                        if f[-3:] == ".py" and f != "__init__.py":
                            try:
                                module_path = os.path.join(root, f)
                                path_components = module_path.split(os.sep)
                                module_name = path_components[-1][:-3]
                                full_module_name = ".".join(path_components)

                                # Check blacklist.
                                blacklisted = False
                                for b in settings.PLUGIN_BLACKLIST:
                                    if b in full_module_name:
                                        blacklisted = True
                                        break

                                parent_mod = path_components[-2].split("/")[-1]
                                parent_help_text = parent_mod.title()
                                # Don't even *try* to load a blacklisted module.
                                if not blacklisted:
                                    try:
                                        plugin_modules[full_module_name] = imp.load_source(module_name, module_path)

                                        parent_root = os.path.join(root, "__init__.py")
                                        parent = imp.load_source(parent_mod, parent_root)
                                        parent_help_text = getattr(parent, "MODULE_DESCRIPTION", parent_help_text)
                                    except:
                                        # If it's blacklisted, don't worry if this blows up.
                                        if blacklisted:
                                            pass
                                        else:
                                            raise

                                plugin_modules_library[full_module_name] = {
                                    "full_module_name": full_module_name,
                                    "file_path": module_path,
                                    "name": module_name,
                                    "parent_name": plugin_name,
                                    "parent_module_name": parent_mod,
                                    "parent_help_text": parent_help_text,
                                    "blacklisted": blacklisted,
                                }
                            except Exception, e:
                                self.startup_error("Error loading %s" % (module_path,), e)

                self.plugins = []
                for name, module in plugin_modules.items():
                    try:
                        for class_name, cls in inspect.getmembers(module, predicate=inspect.isclass):
                            try:
                                if hasattr(cls, "is_will_plugin") and cls.is_will_plugin and class_name != "WillPlugin":
                                    self.plugins.append({
                                        "name": class_name,
                                        "class": cls,
                                        "module": module,
                                        "full_module_name": name,
                                        "parent_name": plugin_modules_library[name]["parent_name"],
                                        "parent_module_name": plugin_modules_library[name]["parent_module_name"],
                                        "parent_help_text": plugin_modules_library[name]["parent_help_text"],
                                        "blacklisted": plugin_modules_library[name]["blacklisted"],
                                    })
                            except Exception, e:
                                self.startup_error("Error bootstrapping %s" % (class_name,), e)
                    except Exception, e:
                        self.startup_error("Error bootstrapping %s" % (name,), e)

            self._plugin_modules_library = plugin_modules_library

            # Sift and Sort.
            self.message_listeners = {}
            self.periodic_tasks = []
            self.random_tasks = []
            self.bottle_routes = []
            self.all_listener_regexes = []
            self.help_modules = {}
            self.help_modules[OTHER_HELP_HEADING] = []
            self.some_listeners_include_me = False
            self.plugins.sort(key=operator.itemgetter("parent_module_name"))
            self.required_settings_from_plugins = {}
            last_parent_name = None
            for plugin_info in self.plugins:
                try:
                    if last_parent_name != plugin_info["parent_help_text"]:
                        friendly_name = "%(parent_help_text)s " % plugin_info
                        module_name = " %(parent_name)s" % plugin_info
                        # Justify
                        friendly_name = friendly_name.ljust(50, '-')
                        module_name = module_name.rjust(40, '-')
                        puts("")
                        puts("%s%s" % (friendly_name, module_name))

                        last_parent_name = plugin_info["parent_help_text"]
                    with indent(2):
                        plugin_name = plugin_info["name"]
                        # Just a little nicety
                        if plugin_name[-6:] == "Plugin":
                            plugin_name = plugin_name[:-6]
                        if plugin_info["blacklisted"]:
                            puts("✗ %s (blacklisted)" % plugin_name)
                        else:
                            plugin_instances = {}
                            for function_name, fn in inspect.getmembers(
                                plugin_info["class"],
                                predicate=inspect.ismethod
                            ):
                                try:
                                    # Check for required_settings
                                    with indent(2):
                                        if hasattr(fn, "will_fn_metadata"):
                                            meta = fn.will_fn_metadata
                                            if "required_settings" in meta:
                                                for s in meta["required_settings"]:
                                                    self.required_settings_from_plugins[s] = {
                                                        "plugin_name": plugin_name,
                                                        "function_name": function_name,
                                                        "setting_name": s,
                                                    }
                                            if (
                                                "listens_to_messages" in meta and
                                                meta["listens_to_messages"] and
                                                "listener_regex" in meta
                                            ):
                                                # puts("- %s" % function_name)
                                                regex = meta["listener_regex"]
                                                if not meta["case_sensitive"]:
                                                    regex = "(?i)%s" % regex
                                                help_regex = meta["listener_regex"]
                                                if meta["listens_only_to_direct_mentions"]:
                                                    help_regex = "@%s %s" % (settings.HANDLE, help_regex)
                                                self.all_listener_regexes.append(help_regex)
                                                if meta["__doc__"]:
                                                    pht = plugin_info.get("parent_help_text", None)
                                                    if pht:
                                                        if pht in self.help_modules:
                                                            self.help_modules[pht].append(meta["__doc__"])
                                                        else:
                                                            self.help_modules[pht] = [meta["__doc__"]]
                                                    else:
                                                        self.help_modules[OTHER_HELP_HEADING].append(meta["__doc__"])
                                                if meta["multiline"]:
                                                    compiled_regex = re.compile(regex, re.MULTILINE | re.DOTALL)
                                                else:
                                                    compiled_regex = re.compile(regex)

                                                if plugin_info["class"] in plugin_instances:
                                                    instance = plugin_instances[plugin_info["class"]]
                                                else:
                                                    instance = plugin_info["class"](bot=self)
                                                    plugin_instances[plugin_info["class"]] = instance

                                                full_method_name = "%s.%s" % (plugin_info["name"], function_name)
                                                self.message_listeners[full_method_name] = {
                                                    "full_method_name": full_method_name,
                                                    "function_name": function_name,
                                                    "class_name": plugin_info["name"],
                                                    "regex_pattern": meta["listener_regex"],
                                                    "regex": compiled_regex,
                                                    "fn": getattr(instance, function_name),
                                                    "args": meta["listener_args"],
                                                    "include_me": meta["listener_includes_me"],
                                                    "direct_mentions_only": meta["listens_only_to_direct_mentions"],
                                                    "admin_only": meta["listens_only_to_admin"],
                                                    "acl": meta["listeners_acl"],
                                                }
                                                if meta["listener_includes_me"]:
                                                    self.some_listeners_include_me = True
                                            elif "periodic_task" in meta and meta["periodic_task"]:
                                                # puts("- %s" % function_name)
                                                self.periodic_tasks.append((plugin_info, fn, function_name))
                                            elif "random_task" in meta and meta["random_task"]:
                                                # puts("- %s" % function_name)
                                                self.random_tasks.append((plugin_info, fn, function_name))
                                            elif "bottle_route" in meta:
                                                # puts("- %s" % function_name)
                                                self.bottle_routes.append((plugin_info["class"], function_name))
                                except Exception, e:
                                    error(plugin_name)
                                    self.startup_error(
                                        "Error bootstrapping %s.%s" % (
                                            plugin_info["class"],
                                            function_name,
                                        ), e
                                    )
                            show_valid(plugin_name)
                except Exception, e:
                    self.startup_error("Error bootstrapping %s" % (plugin_info["class"],), e)
        puts("")
