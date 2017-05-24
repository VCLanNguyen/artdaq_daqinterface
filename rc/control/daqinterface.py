#!/bin/env python

import os
import sys
sys.path.append( os.getcwd() )

import argparse
import datetime
import subprocess
from subprocess import Popen
from time import sleep, time
import traceback
import re
import random
import string
import glob
import stat
from threading import Thread
import shutil
import socket

from rc.io.timeoutclient import TimeoutServerProxy
from rc.control.component import Component 
from rc.control.deepsuppression import deepsuppression

from rc.control.config_functions_local import get_config_info_base
from rc.control.config_functions_local import put_config_info_base
from rc.control.config_functions_local import get_daqinterface_config_info_base
from rc.control.config_functions_local import listdaqcomps_base
from rc.control.config_functions_local import listconfigs_base
from rc.control.save_run_record import save_run_record_base
from rc.control.save_run_record import total_events_in_run_base
from rc.control.save_run_record import save_metadata_value_base
from rc.control.start_datataking_noop import start_datataking_base
from rc.control.stop_datataking_noop import stop_datataking_base
from rc.control.bookkeeping import bookkeeping_for_fhicl_documents_artdaq_v2_base

from rc.control.online_monitoring import launch_art_procs_base
from rc.control.online_monitoring import kill_art_procs_base

from rc.control.utilities import expand_environment_variable_in_string
from rc.control.utilities import make_paragraph
from rc.control.utilities import get_pids
from rc.control.utilities import is_msgviewer_running

class DAQInterface(Component):
    """
    DAQInterface: The intermediary between Run Control, the
    configuration database, and artdaq processes

    """

    # "Procinfo" is basically just a simple structure containing all
    # the info about a given artdaq process that DAQInterface might
    # care about

    # However, it also contains a less-than function which allows it
    # to be sorted s.t. processes you'd want shutdown first appear
    # before processes you'd want shutdown last (in order:
    # boardreader, eventbuilder, aggregator)

    # JCF, Nov-17-2015

    # I add the "fhicl_file_path" variable, which is a sequence of
    # paths which are searched in order to cut-and-paste #include'd
    # files (see also the description of the DAQInterface class's
    # fhicl_file_path variable, whose sole purpose is to be passed to
    # Procinfo's functions)

    class Procinfo(object):
        def __init__(self, name, host, port, fhicl=None, fhicl_file_path = []):
            self.name = name
            self.port = port
            self.host = host
            self.fhicl = fhicl     # Name of the input FHiCL document
            self.ffp = fhicl_file_path
            self.priority = 999

            # FHiCL code actually sent to the process

            # JCF, 11/11/14 -- note that "fhicl_used" will be modified
            # during the initalization function, as bookkeeping, etc.,
            # is performed on FHiCL parameters

            if self.fhicl is not None:
                self.fhicl_used = ""
                self.recursive_include(self.fhicl)
            else:
                self.fhicl_used = None

            # JCF, Jan-14-2016

            # Do NOT change the "lastreturned" string below without
            # changing the commensurate string in check_proc_transition!

            self.lastreturned = "DAQInterface: ARTDAQ PROCESS NOT YET CALLED"
            self.socketstring = "http://" + self.host + ":" + self.port \
                + "/RPC2"

        def update_fhicl(self, fhicl):
            self.fhicl = fhicl
            self.fhicl_used = ""
            self.recursive_include(self.fhicl)

        def __lt__(self, other):
            if self.name != other.name:

                processes_upstream_to_downstream = \
                    ["BoardReader", "EventBuilder", "Aggregator", "DataLogger", "Dispatcher"]

                if processes_upstream_to_downstream.index(self.name) < \
                        processes_upstream_to_downstream.index(other.name):
                    return True
                else:
                    return False
            else:
                if int(self.port) < int(other.port):
                    return True
                return False

        def recursive_include(self, filename):
            if self.fhicl is not None:            
                for line in open(filename).readlines():

                    if "#include" not in line:
                        self.fhicl_used += line
                    else:
                        res = re.search(r"^\s*#.*#include", line)

                        if res:
                            self.fhicl_used += line
                            continue

                        res = re.search(r"^\s*#include\s+\"(\S+)\"", line)
                        
                        if not res:
                            raise Exception(make_paragraph("Error in Procinfo::recursive_include: "
                                            "unable to parse line \"%s\" in %s" %
                                            (line, filename)))

                        included_file = res.group(1)

                        if included_file[0] == "/":
                            if not os.path.exists(included_file):
                                raise Exception(make_paragraph("Error in "
                                                                    "Procinfo::recursive_include: "
                                                                    "unable to find file %s included in %s" %
                                                               (included_file, filename)))
                            else:
                                self.recursive_include(included_file)
                        else:
                            found_file = False
                            
                            for dirname in self.ffp:
                                if os.path.exists( dirname + "/" + included_file) and not found_file:
                                    self.recursive_include(dirname + "/" + included_file)
                                    found_file = True

                            if not found_file:
                                
                                ffp_string = ":".join(self.ffp)

                                raise Exception(make_paragraph(
                                        "Error in Procinfo::recursive_include: "
                                        "unable to find file %s in list of "
                                        "the following fhicl_file_paths: %s" %
                                        (included_file, ffp_string)))
                            
    def date_and_time(self):
        return Popen("date", shell=True, stdout=subprocess.PIPE).stdout.readlines()[0].strip()

    def print_log(self, printstr, debuglevel=-999):
#        self.logger.log(printstr)

        if self.debug_level >= debuglevel:
            print "%s: %s" % (self.date_and_time(), printstr)

    def construct_checked_command(self, cmds ):

        checked_cmds = []

        for cmd in cmds:
            checked_cmds.append( cmd )

            if not re.search(r"\s*&\s*$", cmd):
                check_cmd = " if [[ \"$?\" != \"0\" ]]; then echo Nonzero return value from command \"%s\" ; exit 1; fi " % (cmd)
                checked_cmds.append( check_cmd )

        total_cmd = " ; ".join( checked_cmds )

        return total_cmd

    # JCF, Dec-16-2016

    # The purpose of reset_variables is to reset those members that
    # (A) aren't necessarily persistent to the process (thus excluding
    # the paramters in .settings) and (B) won't necessarily be set
    # explicitly during the transitions up from the "stopped"
    # state. E.g., you wouldn't want to return to the "stopped" state
    # with self.exception == True and then try a boot-config-start
    # without self.exception being reset to False

    def reset_variables(self):

        self.exception = False
        self.in_recovery = False
        self.heartbeat_failure = False
        self.last_artdaq_line = None

        # "procinfos" will be an array of Procinfo structures (defined
        # below), where Procinfo contains all the info DAQInterface
        # needs to know about an individual artdaq process: name,
        # host, port, and FHiCL initialization document. Filled
        # through a combination of info in the DAQInterface
        # configuration file as well as the components list

        self.procinfos = []

    # Constructor for DAQInterface begins here

    def __init__(self, logpath=None, name="toycomponent",
                 rpc_host="localhost", control_host='localhost',
                 synchronous=True, rpc_port=6659):

        # Initialize Component, the base class of DAQInterface

        Component.__init__(self, logpath=logpath,
                           name=name,
                           rpc_host=rpc_host,
                           control_host=control_host,
                           synchronous=synchronous,
                           rpc_port=rpc_port,
                           skip_init=False)

        self.manage_processes = True

        self.in_recovery = False
        self.heartbeat_failure = False

        self.daqinterface_base_dir = os.getcwd()
            
        # JCF, Nov-17-2015

        # fhicl_file_path is a sequence of directory names which will
        # be searched for any FHiCL documents #include'd by the main
        # document used to initialize each artdaq process, but not
        # given with an absolute path in the #include .

        self.fhicl_file_path = []

        # JCF, Nov-7-2015

        # Now that we're going with a multithreaded (simultaneous)
        # approach to sending transition commands to artdaq processes,
        # when an exception is thrown a thread the main thread needs
        # to know about it somehow - thus this new exception variable

        self.exception = False

        # This will contain the directory with the FHiCL documents
        # which initialize the artdaq processes

        self.config_dirname = None

        # This keeps a record of the last line presented by the
        # display_artdaq_output() function, so it isn't
        # repeatedly printed to screen

        self.last_artdaq_line = None

        self.__do_boot = False
        self.__do_shutdown = False
        self.__do_config = False
        self.__do_start_running = False
        self.__do_stop_running = False
        self.__do_terminate = False
        self.__do_pause_running = False
        self.__do_resume_running = False
        self.__do_recover = False

        try:
            self.read_settings()
        except:
            print traceback.format_exc()
            print make_paragraph(
                    "An exception was thrown when trying to read DAQInterface settings; "
                    "DAQInterface will exit. Look at the messages above, make any necessary "
                    "changes, and restart.") + "\n"
            sys.exit(1)

        print make_paragraph("DAQInterface launched and now in \"%s\" state" % 
                                  (self.state(self.name)))

    get_config_info = get_config_info_base
    put_config_info = put_config_info_base
    get_daqinterface_config_info = get_daqinterface_config_info_base
    listdaqcomps = listdaqcomps_base
    listconfigs = listconfigs_base
    save_run_record = save_run_record_base
    total_events_in_run = total_events_in_run_base
    save_metadata_value = save_metadata_value_base
    start_datataking = start_datataking_base
    stop_datataking = stop_datataking_base
    bookkeeping_for_fhicl_documents = bookkeeping_for_fhicl_documents_artdaq_v2_base
    launch_art_procs = launch_art_procs_base
    kill_art_procs = kill_art_procs_base

    # The actual transition functions called by Run Control; note
    # these just set booleans which are tested in the runner()
    # function, called periodically by run control

    def boot(self):
        self.__do_boot = True

    def shutdown(self):
        self.__do_shutdown = True

    def config(self):
        self.__do_config = True

    def recover(self):
        self.__do_recover = True

    def start_running(self):
        self.__do_start_running = True

    def stop_running(self):
        self.__do_stop_running = True

    def terminate(self):
        self.__do_terminate = True

    def pause_running(self):
        self.__do_pause_running = True

    def resume_running(self):
        self.__do_resume_running = True

    def alert_and_recover(self, extrainfo=None):

        self.do_recover()
                
        alertmsg = ""
        
        if not extrainfo is None:
            alertmsg = "\n\n" + make_paragraph( "\"" + extrainfo + "\"")

        alertmsg += "\n" + make_paragraph("DAQInterface has set the DAQ back in the \"stopped\" state; please make any necessary adjustments suggested by the messages above.")
        self.print_log( alertmsg )

    def read_settings(self):
        if not os.path.exists( os.getcwd() + "/.settings"):

            raise Exception(make_paragraph("""Unable to find \".settings\" file in current directory
\"%s\"; this is probably because you're not running DAQInterface out of its package's base directory.
Please kill DAQInterface and run it out of the base directory.""" % \
                        os.getcwd()))

        inf = open( os.getcwd() + "/.settings" )
        assert inf

        self.log_directory = None
        self.record_directory = None
        self.daq_setup_script = None
        self.package_hashes_to_save = None

        self.boardreader_timeout = 30
        self.eventbuilder_timeout = 30
        self.aggregator_timeout = 30

        for line in inf.readlines():

            line = expand_environment_variable_in_string( line )

            if re.search(r"^\s*#", line):
                continue
            elif "log_directory" in line:
                self.log_directory = line.split()[-1].strip()
            elif "record_directory" in line:
                self.record_directory = line.split()[-1].strip()
            elif "daq_setup_script" in line:
                self.daq_setup_script = line.split()[-1].strip()
            elif "package_hashes_to_save" in line:
                res = re.search(r".*\[(.*)\].*", line)

                if not res:
                    raise Exception(make_paragraph(
                            "Unable to parse package_hashes_to_save line in the settings file, %s" % \
                                (os.getcwd() + "/.settings")))

                self.package_hashes_to_save = []

                if res.group(1).strip() == "":
                    continue

                package_hashes_to_save_unprocessed = res.group(1).split(",")

                for ip, package in enumerate(package_hashes_to_save_unprocessed):
                    package = string.replace(package, "\"", "")
                    package = string.replace(package, " ", "") # strip() doesn't seem to work here
                    self.package_hashes_to_save.append(package)
            elif "boardreader timeout" in line:
                self.boardreader_timeout = int( line.split()[-1].strip() )
            elif "eventbuilder timeout" in line:
                self.eventbuilder_timeout = int( line.split()[-1].strip() )
            elif "aggregator timeout" in line:
                self.aggregator_timeout = int( line.split()[-1].strip() )
            elif "boardreader priorities" in line:
                self.boardreader_priorities = [regexp.strip() for regexp in line.split()[2:] if ":" not in regexp]
            elif "max_fragment_size_bytes" in line:
                self.max_fragment_size_bytes = int( line.split()[-1].strip())
                if self.max_fragment_size_bytes % 8 != 0:
                    raise Exception("Value for \"max_fragment_size_bytes\" in .settings should be a multiple of 8")
            elif "all_events_to_all_dispatchers" in line:
                token = line.split()[-1].strip()
                
                if "true" in token or "True" in token:
                    self.all_events_to_all_dispatchers = True
                elif "false" in token or "False" in token:
                    self.all_events_to_all_dispatchers = False
                else:
                    raise Exception("all_events_to_all_dispatchers must be set to either [Tt]rue or [Ff]alse")

        missing_vars = []

        if self.log_directory is None:
            missing_vars.append("log_directory")
            
        if self.record_directory is None:
            missing_vars.append("record_directory")

        if self.daq_setup_script is None:
            missing_vars.append("daq_setup_script")

        if self.package_hashes_to_save is None or self.package_hashes_to_save is []:
            missing_vars.append("package_hashes_to_save")

        if len(missing_vars) > 0:
            missing_vars_string = ", ".join(missing_vars)
            print
            raise Exception(make_paragraph(
                                "Unable to parse the following variable(s) meant to be set in the "
                                "settings file, %s" % \
                                    (os.getcwd() + "/.settings : " + missing_vars_string ) ))
        
                    

    def check_proc_transition(self, target_state):

        is_all_ok = True
        
        # The following code will give artdaq processes max_retries
        # chances to return "Success", if, rather than
        # procinfo.lastreturned indicating an error condition, it
        # simply appears that it hasn't been assigned its new status
        # yet

        for procinfo in self.procinfos:

            if procinfo.lastreturned != "Success" and procinfo.lastreturned != target_state:

                redeemed=False
                max_retries=20
                retry_counter=0
                
                while retry_counter < max_retries and ( 
                    "ARTDAQ PROCESS NOT YET CALLED" in procinfo.lastreturned or
                    "Stopped" in procinfo.lastreturned or
                    "Booted" in procinfo.lastreturned or
                    "Ready" in procinfo.lastreturned or
                    "Running" in procinfo.lastreturned or
                    "Paused" in procinfo.lastreturned or
                    "busy" in procinfo.lastreturned):
                    retry_counter += 1
                    sleep(1)
                    if procinfo.lastreturned  == "Success" or procinfo.lastreturned == target_state:
                        redeemed=True

                if redeemed:
                    successmsg = "After " + str(retry_counter) + " checks, process " + \
                        procinfo.name + " at " + procinfo.host + ":" + procinfo.port + " returned \"Success\""
                    self.print_log( successmsg )
                    continue  # We're fine, continue on to the next process check

                errmsg = "Unexpected status message from process " + procinfo.name + " at " + procinfo.host + \
                    ":" + procinfo.port + ": \"" + \
                    procinfo.lastreturned + "\""
                self.print_log(make_paragraph(errmsg))

                is_all_ok = False

        if not is_all_ok:
            raise Exception("At least one artdaq process failed a transition")



    # Utility functions used to count the different process types

    def num_boardreaders(self):
        num_boardreaders = 0
        for procinfo in self.procinfos:
            if "BoardReader" in procinfo.name:
                num_boardreaders += 1
        return num_boardreaders

    def num_eventbuilders(self):
        num_eventbuilders = 0
        for procinfo in self.procinfos:
            if "EventBuilder" in procinfo.name:
                num_eventbuilders += 1
        return num_eventbuilders

    def num_aggregators(self):
        num_aggregators = 0
        for procinfo in self.procinfos:
            if "Aggregator" in procinfo.name or "DataLogger" in procinfo.name \
                    or "Dispatcher" in procinfo.name:
                num_aggregators += 1
        return num_aggregators

    def artdaq_mfextensions_info(self):

        product_deps_filename = "%s/srcs/artdaq/ups/product_deps" % (self.daq_dir)

        if not os.path.exists( product_deps_filename ):
            raise Exception("Unable to find artdaq product_deps file \"%s\"; needed to determine artdaq_mfextensions version for messagefacility viewer")
            return

        product_deps_file = open( product_deps_filename )

        lines = product_deps_file.readlines()

        for line in lines:
            res = re.search(r"^\s*defaultqual\s+(e[0-9]+):(s[0-9]+)", line)
            if res:
                equalifier = res.group(1)
                squalifier = res.group(2)

            res = re.search(r"^\s*artdaq_mfextensions\s+([v_0-9]+)", line)
            if res:
                version = res.group(1)

        return (version, equalifier, squalifier)
    
    def have_needed_artdaq_mfextensions(self):

        version, equalifier, squalifier = self.artdaq_mfextensions_info()

        cmds = []
        cmds.append("cd %s" % (self.daq_dir))
        cmds.append(". ./%s" % (self.daq_setup_script))
        cmds.append('if [[ "$ARTDAQ_MFEXTENSIONS_VERSION" == "%s" ]]; then true; else false; fi' % \
                        (version))

        checked_cmd = self.construct_checked_command( cmds )
        
        with deepsuppression():
            status = Popen(checked_cmd, shell = True).wait()

        if status == 0:
            return True
        else:
            return False

    # JCF, 8/11/14

    # launch_procs() will create the artdaq processes

    def launch_procs(self):

        greptoken = "pmt.rb -p " + self.pmt_port
        pids = get_pids(greptoken, self.pmt_host)

        if len(pids) != 0:
            raise Exception("\"pmt.rb -p %s\" was already running on %s" %
                            (self.pmt_port, self.pmt_host))

        if self.debug_level > 1:

            print "DAQInterface: will launch " + \
                str(self.num_boardreaders()) + \
                " BoardReaderMain processes, " + \
                str(self.num_eventbuilders()) + \
                " EventBuilderMain processes, and " + \
                str(self.num_aggregators()) + \
                " AggregatorMain processes"

            print "Assuming daq package is in " + \
                self.daq_dir

        # We'll use the desired features of the artdaq processes to
        # create a text file which will be passed to artdaq's pmt.rb
        # program

        pmtconfigname = "/tmp/pmtConfig." + \
            ''.join(random.choice(string.digits)
                    for _ in range(5))

        outf = open(pmtconfigname, "w")

        for procinfo in self.procinfos:

            for procname in ["BoardReader", "EventBuilder", "Aggregator"]:
                if procname in procinfo.name:
                    outf.write(procname + "Main ")

            for procname in ["DataLogger", "Dispatcher"]:
                if procname in procinfo.name:
                    outf.write("AggregatorMain ")

            if procinfo.host != "localhost":
                host_to_write = procinfo.host
            else:
                host_to_write = os.environ["HOSTNAME"]

            outf.write(host_to_write + " " + procinfo.port + "\n")

        outf.close()

        if self.pmt_host != "localhost" and self.pmt_host != os.environ["HOSTNAME"]:
            status = Popen("scp -p " + pmtconfigname + " " +
                           self.pmt_host + ":/tmp", shell=True).wait()

            if status != 0:
                raise Exception("Exception in DAQInterface: unable to copy " +
                                pmtconfigname + " to " + self.pmt_host + ":/tmp")

        self.launch_cmds = []

        for logdir in ["pmt", "masterControl", "boardreader", "eventbuilder",
                       "aggregator"]:
            self.launch_cmds.append("mkdir -p -m 0777 " + self.log_directory +
                                    "/" + logdir)

        self.launch_cmds.append("cd " + self.daq_dir)
        self.launch_cmds.append("source ./" + self.daq_setup_script )
        self.launch_cmds.append("which pmt.rb")  # Sanity check capable of returning nonzero

        # 30-Jan-2017, KAB: increased the amount of time that pmt.rb provides daqinterface
        # to react to errors.  This should be longer than the sum of the individual
        # process timeouts.
        self.launch_cmds.append("export ARTDAQ_PROCESS_FAILURE_EXIT_DELAY=120")


        if self.have_needed_artdaq_mfextensions():

            messagefacility_fhicl_filename = os.getcwd() + "/MessageFacility.fcl" 
            
            messagefacility_fhicl_file = open(messagefacility_fhicl_filename, "w")
            messagefacility_fhicl_file.write( 'udp : { type : "UDP" threshold : "INFO" port : 30000 host : "%s" }  ' % (socket.gethostname()) )
            messagefacility_fhicl_file.close()

            cmd = "pmt.rb -p " + self.pmt_port + " -d " + pmtconfigname + \
                " --logpath " + self.log_directory + \
                " --logfhicl " + messagefacility_fhicl_filename + " --display $DISPLAY & "
        else:

            cmd = "pmt.rb -p " + self.pmt_port + " -d " + pmtconfigname + \
                " --logpath " + self.log_directory + \
                " --display $DISPLAY & "
   
        self.launch_cmds.append(cmd)

        launchcmd = self.construct_checked_command( self.launch_cmds )

        if self.pmt_host != "localhost" and self.pmt_host != os.environ["HOSTNAME"]:
            launchcmd = "ssh -f " + self.pmt_host + " '" + launchcmd + "'"

        if self.debug_level >= 2:
            print "PROCESS LAUNCH COMMANDS: "
            print "\n".join( self.launch_cmds )
            print

        if self.debug_level >= 3:
            status = Popen(launchcmd, shell=True).wait()
        else:
            with deepsuppression():
                status = Popen(launchcmd, shell=True).wait()

        if status != 0:
            raise Exception("Status error raised; commands were \"\n%s\n\n\". If logfiles exist, please check them for more information. Also try running the commands interactively in a new terminal for more info." %
                            ("\n".join(self.launch_cmds)))
            return


    # check_proc_heartbeats() will check that the expected artdaq
    # processes are up and running

    def check_proc_heartbeats(self, requireSuccess=True):

        is_all_ok = True

        for procinfo in self.procinfos:

            if "BoardReader" in procinfo.name:
                proctype = "BoardReaderMain"
            elif "EventBuilder" in procinfo.name:
                proctype = "EventBuilderMain"
            elif "Aggregator" in procinfo.name or \
                    "DataLogger" in procinfo.name or \
                    "Dispatcher" in procinfo.name:
                proctype = "AggregatorMain"
            else:
                assert False

            greptoken = proctype + " -p " + procinfo.port

            pids = get_pids(greptoken, procinfo.host)

            num_procs_found = len(pids)

            if num_procs_found != 1:
                is_all_ok = False

                if requireSuccess:
                    errmsg = "Expected process " + procinfo.name + \
                        " at " + procinfo.host + ":" + \
                        procinfo.port + " not found"

#                    self.print_log(
#                        make_paragraph("Error in DAQInterface::check_proc_heartbeats(): "
#                                            "please check messageviewer and/or the logfiles for error messages"))
                    self.print_log(errmsg)

        if not is_all_ok and requireSuccess:
            self.heartbeat_failure = True
            self.alert_and_recover("At least one artdaq process died unexpectedly; please check messageviewer"
                                   " and/or the logfiles for error messages")
            return

        return is_all_ok

    # JCF, 5/29/15

    # check_proc_exceptions() takes advantage of an artdaq feature
    # developed by Kurt earlier this month whereby if something goes
    # wrong in an artdaq process during running (e.g., a fragment
    # generator's getNext_() function throws an exception) then, when
    # queried, the artdaq process can return an "Error" state, as
    # opposed to the usual DAQ states ("Ready", "Running", etc.)

    # Feb-26-2017

    # Note that "exceptions" in the context of the function name
    # check_proc_exceptions() refers to an exception being thrown
    # within a fragment generator, resulting in the artdaq process
    # returning an "Error" when queried. It's not the same thing as
    # what the self.exception variable denotes, which is that a
    # literal Python exception got thrown at some point.

    def check_proc_exceptions(self):

        if self.exception:
            return

        is_all_ok = True

        for procinfo in self.procinfos:

            try:
                procinfo.lastreturned = procinfo.server.daq.status()
            except Exception:
                self.exception = True
                exceptstring = make_paragraph("Exception caught in DAQInterface attempt to query status of artdaq process %s at %s:%s; most likely reason is process no longer exists" % \
                    (procinfo.name, procinfo.host, procinfo.port))              
                self.print_log(exceptstring)

            if procinfo.lastreturned == "Error":
                is_all_ok = False
                errmsg = "\"Error\" state returned by process %s at %s:%s; please check messageviewer and/or the logfiles for error messages" % \
                    (procinfo.name, procinfo.host, procinfo.port)

                self.print_log(make_paragraph(errmsg))

        if not is_all_ok:
            self.alert_and_recover("One or more artdaq processes"
                                   " discovered to be in \"Error\" state")
            return

    # JCF, 1/28/15

    # The idea behind the "display_artdaq_output()" function is
    # that in the runner() function, after checking that all artdaq
    # processes are alive, the PMT logfile is then examined for
    # red-flag terms like "exception" and "error", and any lines with
    # these terms get displayed

    def display_artdaq_output(self):

        keywords = ["error", "exception", "back-pressure", "MSG-e", "errno"]

        grepstring = "\|".join(keywords)

        cmds = []

        cmds.append("cd %s/pmt" % (self.log_directory))
        cmds.append("most_recent_logfile=$(ls -tr1 %s )" %
                    (self.log_filename_wildcard))

        # JCF, 5/1/15

        # Want to avoid accidentally grepping an old pmt logfile which
        # happens to share the same process ID as the current one

        cmds.append("if [[ $(find \"$most_recent_logfile\" -mmin -60) ]];"
                    " then grep -i1 \"%s\" $most_recent_logfile; fi"
                    % (grepstring))

        cmd = ";".join(cmds)

        if self.pmt_host != "localhost":
            cmd = "ssh -f " + self.pmt_host + " '" + cmd + "'"

        status = Popen(cmd, shell=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)

        lines = status.stdout.readlines()

        if len(lines) > 1:

            # "1" because we're using the "-1" option to grep
            line = lines[1].strip()

            if line != self.last_artdaq_line:
                self.last_artdaq_line = line

                for tmpline in lines:
                    print tmpline.strip()


    def kill_procs(self):

        # JCF, 12/29/14

        # If the PMT host hasn't been defined, we can be sure there
        # aren't yet any artdaq processes running yet (or at least, we
        # won't be able to determine where they're running!)

        if self.pmt_host is None:
            return

        # Now, the commands which will clean up the pmt.rb + its child
        # artdaq processes

        pmt_pids = get_pids("ruby.*pmt.rb -p " + str(self.pmt_port),
                                 self.pmt_host)

        if len(pmt_pids) > 0:

            for pmt_pid in pmt_pids:

                cmd = "kill %s; sleep 2; kill -9 %s" % (pmt_pid, pmt_pid)

                if self.pmt_host != "localhost":
                    cmd = "ssh -f " + self.pmt_host + " '" + cmd + "'"

                proc = Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        for procinfo in self.procinfos:
            
            if procinfo.name == "DataLogger" or procinfo.name == "Dispatcher":
                greptoken = "AggregatorMain -p " + procinfo.port
            else:
                greptoken = procinfo.name + "Main -p " + procinfo.port

            pids = get_pids(greptoken, procinfo.host)

            if len(pids) > 0:
                cmd = "kill -9 " + pids[0]

                if procinfo.host != "localhost":
                    cmd = "ssh -f " + procinfo.host + " '" + cmd + "'"

                Popen(cmd, shell=True, stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT)

                # Check that it was actually killed

                sleep(1)

                pids = get_pids(greptoken, procinfo.host)

                if len(pids) > 0:
                    self.print_log("Appeared to be unable to kill %s at %s:%s during cleanup" % \
                                       (procinfo.name, procinfo.host, procinfo.port))

        self.procinfos = []

        self.kill_art_procs()

        return

    # JCF, 12/2/14

    # Given the directory name of a git repository, this will return
    # the most recent hash commit in the repo

    def get_commit_hash(self, gitrepo):

        if not os.path.exists(gitrepo):
            self.alert_and_recover("Expected git directory %s not found" % (gitrepo))
            return

        cmds = []
        cmds.append("cd %s" % (gitrepo))
        cmds.append("git log | head -1 | awk '{print $2}'")

        proc = Popen(";".join(cmds), shell=True,
                     stdout=subprocess.PIPE)
        proclines = proc.stdout.readlines()

        if len(proclines) != 1 or len(proclines[0].strip()) != 40:
            self.alert_and_recover("Commit hash for %s not found" % (gitrepo))
            return

        return proclines[0].strip()

    def check_daqinterface_config_info(self):

        # Check that the configuration file actually contained the
        # definitions we wanted

        # The BoardReaderMain info should be supplied by the
        # configuration manager; info for both AggregatorMains and the
        # EventBuilderMains (excluding their FHiCL documents) should
        # be supplied in the DAQInterface configuration file

        if self.num_boardreaders() != 0 or \
                self.num_eventbuilders() == 0:
            errmsg = "Unexpected number of artdaq processes provided " \
                "by the DAQInterface config file: " \
                "%d BoardReaderMains, %d EventBuilderMains " \
                "(expect 0 BoardReaderMains, >0 EventBuilderMains)" % \
                (self.num_boardreaders(),
                 self.num_eventbuilders())

            raise Exception(make_paragraph(errmsg))

        undefined_var = ""

        if self.pmt_host is None:
            undefined_var = "PMT host"
        if self.pmt_port is None:
            undefined_var = "PMT port"
        elif self.daq_dir is None:
            undefined_var = "DAQ directory"
        elif self.debug_level is None:
            undefined_var = "debug level"

        if undefined_var != "":
            errmsg = "Error: \"%s\" undefined in " \
                "DAQInterface config file" % \
                (undefined_var)
            raise Exception(make_paragraph(errmsg))

        if not os.path.exists(self.daq_dir):
            raise Exception("Unable to find requested daq directory \"%s\"" % self.daq_dir)

        if not os.path.exists(self.daq_dir + "/" + self.daq_setup_script ):
            raise Exception(make_paragraph(
                                self.daq_setup_script + " script not found in " +
                                self.daq_dir))


    # JCF, Dec-1-2016

    # Define the local function "get_logfilenames()" which will enable
    # to get the artdaq-process-specific logfiles 

    def get_logfilenames(self, procname):

        logfilenames = []

        host_count = {}

        if procname == "BoardReader":
            subdir = "boardreader"
        elif procname == "EventBuilder":
            subdir = "eventbuilder"
        elif procname == "Aggregator":
            subdir = "aggregator"
        else:
            assert False

        translator = { "BoardReader":"BoardReader", 
                       "EventBuilder":"EventBuilder",
                       "Aggregator":"Aggregator",
                       "DataLogger":"Aggregator",
                       "Dispatcher":"Aggregator" }

        for procinfo in self.procinfos:
            if (procname == translator[ procinfo.name ] ):
                if procinfo.host in host_count.keys():
                    host_count[procinfo.host] += 1
                else:
                    host_count[procinfo.host] = 1

        for host, count in host_count.items():
            cmd = "ls -tr1 %s/%s/%s-*.log | tail -%d" % (self.log_directory,
                                                         subdir, subdir, count)

            if host != "localhost" and host != os.environ["HOSTNAME"]:
                cmd = "ssh -f " + host + " '" + cmd + "'"

            max_num_checks = 5
            num_checks = 0
            pause_between_checks = 2

            while True:
                proc = Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                proclines = proc.stdout.readlines()

                if len(proclines) != count:

                    if num_checks < max_num_checks:
                        num_checks += 1
                    
                        if self.debug_level >= 2:
                            print "Didn't find expected logfiles with command \"%s\"; will wait %d seconds and then check again" % \
                                (cmd, pause_between_checks)

                        sleep(pause_between_checks)
                        continue
                    else:
                        raise Exception("Exception in DAQInterface: " + \
                                            "command \"%s\" on host \"%s\" yielded didn't print the expected number of logfiles. Have the logfile directories been created?" % (cmd, host))
                else:
                    break

            for line in proclines:

                if host == "localhost":
                    host_to_record = os.environ["HOSTNAME"]
                else:
                    host_to_record = host

                logfilenames.append("%s:%s" % (host_to_record, line.strip()))

        return logfilenames

    # JCF, Aug-12-2016

    # get_run_documents is intended to be called after the FHiCL
    # documents have been fully formatted and are ready to send to the
    # artdaq processes; essentially, this just creates a big string
    # which FHiCL can parse but doesn't actually use, and which
    # contains all the FHiCL documents used for all processes, as well
    # as other information pertinent to the run (its metadata output
    # file, etc.). This string is intended to be concatenated at the
    # end of the diskwriting aggregator FHiCL document(s) so that any
    # output file from the DAQ will have a full record of how the DAQ
    # was configured when the file was created when "config_dumper -P
    # <rootfile>" is run

    def get_run_documents(self):

        runstring = "\n\nrun_documents: {\n"
        
        for procinfo in self.procinfos:

            fhicl_readable_hostname = procinfo.host
            fhicl_readable_hostname = fhicl_readable_hostname.replace(".","_")
            fhicl_readable_hostname = fhicl_readable_hostname.replace("-","_")

            runstring += "\n\n  " + procinfo.name.upper() + "_" + fhicl_readable_hostname + \
                "_" + str(procinfo.port) + ": '\n"

            dressed_fhicl = re.sub("'","\"", procinfo.fhicl_used)
            runstring += dressed_fhicl
            runstring += "\n  '\n"
        
        def get_arbitrary_file(filename, label):
            try:
                file = open(filename)
            except:
                self.alert_and_recover("Exception in DAQInterface: unable to find file \"%s\"" % 
                                       (filename))
                return "999"

            contents = "\n  " + label + ": '\n"

            for line in file:
                line = re.sub("'","\\'", line)
                contents += line

            contents += "\n  '\n"
            return contents

        indir = self.tmp_run_record

        metadata_filename = indir + "/metadata.txt"
        runstring += get_arbitrary_file(metadata_filename, "run_metadata")

        config_filename = indir + "/config.txt"        
        runstring += get_arbitrary_file(config_filename, "run_daqinterface_config")

        runstring += "} \n\n"

        return runstring

    # JCF, Nov-8-2015

    # The core functionality for "do_command" is that it will launch a
    # separate thread for each transition issued to an individual
    # artdaq process; for init, start, and resume it will send the
    # command simultaneously to the aggregators, wait for the threads
    # to join, and then do the same thing for the eventbuilders and
    # then the boardreaders. For stop and pause, it will do this in
    # reverse order of upstream/downstream.

    # Note that since "initialize", "start" and "stop" all require
    # additional actions besides simply sending transitions to
    # processes and waiting for their response, "do_command" is not
    # meant to be a replacement for "do_initialize",
    # "do_start_running" and "do_stop_running" the way it IS meant to
    # be a replacement for "do_pause_running", etc., but rather, is
    # meant to be called in the body of those functions. Thus, for
    # those transitions, some functionality (e.g., announding the
    # transition is underway at the beginning of the function, and
    # calling "complete_state_change" at the end) is not applied.

    def do_command(self, command):

        if command != "Start" and command != "Init" and command != "Stop":
            print "\n%s: %s transition underway" % \
                (self.date_and_time(), command.upper())

        # "process_command" is the function which will send a
        # transition to a single artdaq process, and be run on its own
        # thread so that transitions to different processes can be
        # sent simultaneously
                
        # Note that since Python is "pass-by-object-reference" (see
        # http://robertheaton.com/2014/02/09/pythons-pass-by-object-reference-as-explained-by-philip-k-dick/
        # for more), I pass it the index of the procinfo struct we
        # want, rather than the actual procinfo struct

        def process_command(self, procinfo_index, command):

            if self.exception:
                return

            try:

                if command == "Init":
                    if not "Aggregator" in self.procinfos[procinfo_index].name and \
                            not "DataLogger" in self.procinfos[procinfo_index].name:
                        self.procinfos[procinfo_index].lastreturned = \
                            self.procinfos[procinfo_index].server.daq.init(self.procinfos[procinfo_index].fhicl_used)
                    else:
                        self.procinfos[procinfo_index].lastreturned = \
                            self.procinfos[procinfo_index].server.daq.init(self.procinfos[procinfo_index].fhicl_used + self.get_run_documents() )

                elif command == "Start":
                    self.procinfos[procinfo_index].lastreturned = \
                        self.procinfos[procinfo_index].server.daq.start(\
                        str(self.run_number))
                elif command == "Pause":
                    self.procinfos[procinfo_index].lastreturned = \
                        self.procinfos[procinfo_index].server.daq.pause()
                elif command == "Resume":
                    self.procinfos[procinfo_index].lastreturned = \
                        self.procinfos[procinfo_index].server.daq.resume()
                elif command == "Stop":
                    self.procinfos[procinfo_index].lastreturned = \
                        self.procinfos[procinfo_index].server.daq.stop()
                elif command == "Shutdown":
                    self.procinfos[procinfo_index].lastreturned = \
                        self.procinfos[procinfo_index].server.daq.shutdown()
                else:
                    raise Exception("Unknown command")

                if "with ParameterSet" in self.procinfos[procinfo_index].lastreturned:
                    self.procinfos[procinfo_index].lastreturned = self.procinfos[procinfo_index].lastreturned[0:200] + \
                        " // REMAINDER TRUNCATED BY DAQINTERFACE, SEE %s FOR FULL FHiCL DOCUMENT" % (self.tmp_run_record)

            except Exception:
                self.exception = True

                pi = self.procinfos[procinfo_index]

                if "timeout: timed out" in traceback.format_exc():
                    output_message = "Timeout sending %s transition to artdaq process %s at %s:%s \n" % (command, pi.name, pi.host, pi.port)
                else:
                    self.print_log(traceback.format_exc())
                    
                    output_message = "Exception caught sending %s transition to artdaq process %s at %s:%s \n" % (command, pi.name, pi.host, pi.port)

                self.print_log(output_message)
            
            return  # From process_command

        # JCF, Nov-8-2015

        # In the code below, transition commands are sent
        # simultaneously only to classes of artdaq type. So, e.g., if
        # we're stopping, first we send stop to all the boardreaders,
        # next we send stop to all the eventbuilders, and finally we
        # send stop to all the aggregators

        proctypes_in_order = ["Dispatcher", "DataLogger", "Aggregator", "EventBuilder","BoardReader"]

        if command == "Stop" or command == "Pause" or command == "Terminate":
            proctypes_in_order.reverse()

        for proctype in proctypes_in_order:

            threads = []
            priorities_used = {}

            for procinfo in self.procinfos:
                if proctype in procinfo.name:
                    priorities_used[ procinfo.priority ] = "We only care about the key in this dict"

            # JCF, May-24-2017

            # It's not yet clear if reversing the priority of
            # processes WITHIN a process type when halting datataking
            # makes sense the same way it makes sense to reverse the
            # priorities of the process types themselves

            if command == "Stop" or command == "Pause" or command == "Terminate":
                priority_rankings = sorted(priorities_used.iterkeys(), reverse = True)
            else:
                priority_rankings = sorted(priorities_used.iterkeys())

            for priority in priority_rankings:
                for i_procinfo, procinfo in enumerate(self.procinfos):
                    if proctype in procinfo.name and priority == procinfo.priority:
                        t = Thread(target=process_command, args=(self, i_procinfo, command))
                        threads.append(t)
                        t.start()

                for thread in threads:
                    thread.join()

        if self.exception:
            raise Exception("An exception was thrown "
                            "during the %s transition" % (command))

        sleep(1)

        if self.debug_level >= 1:
            for procinfo in self.procinfos:
                print "%s at %s:%s, returned string is: " % \
                    (procinfo.name, procinfo.host, procinfo.port)
                print procinfo.lastreturned
                print

        target_states = {"Init":"Ready", "Start":"Running", "Pause":"Paused", "Resume":"Running",
                         "Stop":"Ready", "Shutdown":"Stopped"}

        try:
            self.check_proc_transition( target_states[ command ] )
        except Exception:
            raise Exception("An exception was thrown during the %s transition as at least one of the artdaq processes didn't achieve its desired state" % (command))


        if command != "Init" and command != "Start" and command != "Stop":

            verbing=""

            if command == "Pause":
                verbing = "pausing"
            elif command == "Resume":
                verbing = "resuming"
            elif command == "Shutdown":
                verbing == "shutting"
            else:
                assert False

            self.complete_state_change(self.name, verbing)
            print "\n%s: %s transition complete" % (self.date_and_time(), command.upper())

    def setdaqcomps(self, daq_comp_list):
        self.daq_comp_list = daq_comp_list

    def revert_failed_transition(self, failed_action):
        self.revert_state_change(self.name, self.state(self.name))
        print (traceback.format_exc())
        print make_paragraph("An exception was thrown when %s; exception has been caught and system remains in the \"%s\" state" % \
                                 (failed_action, self.state(self.name)))
        
    # do_boot(), do_config(), do_start_running(), etc., are the
    # functions which get called in the runner() function when a
    # transition is requested

    def do_boot(self, daqinterface_config = None):

        def revert_failed_boot(failed_action):
            self.reset_variables()            
            self.revert_failed_transition(failed_action)

        print "\n%s: BOOT transition underway" % \
            (self.date_and_time())

        self.reset_variables()
        os.chdir(self.daqinterface_base_dir)

        if not daqinterface_config:
            daqinterface_config = self.run_params["daqinterface_config"]

        try:
            self.daqinterface_config_file = self.get_daqinterface_config_info( daqinterface_config )
            self.check_daqinterface_config_info()
        except Exception:
            revert_failed_boot("when trying to read the DAQInterface configuration \"%s\"" % (daqinterface_config ))
            return

        if not hasattr(self, "daq_comp_list") or not self.daq_comp_list or self.daq_comp_list == {}:
            revert_failed_boot("when checking for the list of components meant to be provided by the \"setdaqcomps\" call")
            return

        self.package_hash_dict = {}

        for pkgname in self.package_hashes_to_save:
            pkg_full_path = "%s/srcs/%s" % (self.daq_dir, pkgname.replace("-", "_"))
            self.package_hash_dict[pkgname] = self.get_commit_hash( pkg_full_path )


        for compname, socket in self.daq_comp_list.items():

            if self.debug_level >= 2:
                print "%s at %s:%s" % (compname, socket[0], socket[1])
 
            self.procinfos.append(self.Procinfo("BoardReader",
                                                socket[0],
                                                socket[1]))

            try:
                for priority, regexp in enumerate(self.boardreader_priorities):
                    if re.search(regexp, compname):
                        self.procinfos[-1].priority = priority

            except Exception:
                pass  # It's not an error if there were no boardreader priorities read in from .settings

        # See the Procinfo.__lt__ function for details on sorting

        self.procinfos.sort()

        if self.manage_processes:

            # Now, with the info on hand about the processes contained in
            # procinfos, actually launch them

            try:
                self.launch_procs()

                if self.debug_level >= 1:
                    print "Finished call to launch_procs(); will now confirm that artdaq processes are up..."

            except Exception:
                self.print_log(traceback.format_exc())

                self.alert_and_recover("An exception was thrown in launch_procs(), see traceback above for more info")
                return

            num_launch_procs_checks = 0

            while True:

                num_launch_procs_checks += 1

                # "False" here means "don't consider it an error if all
                # processes aren't found"

                if self.check_proc_heartbeats(False):

                    if self.debug_level > 0:
                        print "All processes appear to be up"

                    break
                else:
                    if num_launch_procs_checks > 5:
                        print make_paragraph("artdaq processes failed to launch; logfiles may contain info as to what happened. For troubleshooting, you can also try logging into this host via a new terminal, and interactively executing the following commands: ")
                        print "\n".join(self.launch_cmds)
                        self.alert_and_recover("Scroll above the output from the \"RECOVER\" transition for more info")
                        return

            for procinfo in self.procinfos:

                if "BoardReader" in procinfo.name:
                    timeout = self.boardreader_timeout
                elif "EventBuilder" in procinfo.name:
                    timeout = self.eventbuilder_timeout
                elif "Aggregator" in procinfo.name or "DataLogger" in procinfo.name \
                        or "Dispatcher" in procinfo.name:
                    timeout = self.aggregator_timeout

                try:
                    procinfo.server = TimeoutServerProxy(
                        procinfo.socketstring, timeout)
                except Exception:
                    self.print_log(traceback.format_exc())

                    self.alert_and_recover("Problem creating server with socket \"%s\"" % \
                                               procinfo.socketstring)
                    return

        # Figure out if we have the artdaq_mfextensions version expected by the artdaq used 
        
        try:

            version, equalifier, squalifier = self.artdaq_mfextensions_info()

            if self.have_needed_artdaq_mfextensions() and is_msgviewer_running():
                print make_paragraph("An instance of messageviewer already appears to be running; " + \
                                         "messages will be sent to the existing messageviewer")
            elif self.have_needed_artdaq_mfextensions():
                print make_paragraph("artdaq_mfextensions %s, %s:%s, appears to be available; "
                                          "if windowing is supported on your host you should see the "
                                          "messageviewer window pop up momentarily" % \
                                              (version, equalifier, squalifier))

                cmds = []
                cmds.append("cd %s" % (self.daq_dir))
                cmds.append(". ./%s" % (self.daq_setup_script))
                cmds.append("if [[ -n $ARTDAQ_MFEXTENSIONS_FQ_DIR ]]; then export MSGVIEWERDIR=$ARTDAQ_MFEXTENSIONS_FQ_DIR/bin/  ; else export MSGVIEWERDIR=$MRB_BUILDDIR/artdaq_mfextensions/bin ; fi")
                cmds.append("which msgviewer")
                cmds.append("msgviewer -c $MSGVIEWERDIR/msgviewer.fcl 2>&1 > /dev/null &" )

                msgviewercmd = self.construct_checked_command( cmds )

                with deepsuppression():

                    status = Popen(msgviewercmd, shell=True).wait()

                    if status != 0:
                        self.alert_and_recover("Status error raised in msgviewer call within Popen; tried the following commands: \"%s\"" %
                                        " ; ".join(cmds) )
                        return
            else:
                print make_paragraph("artdaq_mfextensions %s, %s:%s, does not appear to be available in the products directory \"%s\" - "
                                          " unable to launch the messageviewer window. This will not affect"
                                          " actual datataking, it just means you'll need to look at the"
                                          " logfiles to see artdaq output." % \
                                              (version, equalifier, squalifier, self.daq_dir + "/products"))

        except Exception:
            self.print_log(traceback.format_exc())
            self.alert_and_recover("Problem during messageviewer launch stage")
            return

        # JCF, 3/5/15

        # Get our hands on the name of logfile so we can save its
        # name for posterity. This is taken to be the most recent
        # logfile found in the log directory. There's a tiny chance
        # someone else's logfile could sneak in during the few seconds
        # taken during startup, but it's unlikely...

        try:

            cmd = "ls -tr1 %s/pmt | tail -1" % (self.log_directory)

            if self.pmt_host != "localhost" and self.pmt_host != os.environ["HOSTNAME"]:
                cmd = "ssh %s '%s'" % (self.pmt_host, cmd)

            log_filename_current = Popen(cmd, shell=True, stdout=subprocess.PIPE).stdout.readlines()[0].strip()

            self.log_filename_wildcard = \
                log_filename_current.split(".")[0] + ".*" + ".log"

            self.boardreader_log_filenames = self.get_logfilenames("BoardReader")
            self.eventbuilder_log_filenames = self.get_logfilenames("EventBuilder")
            self.aggregator_log_filenames = self.get_logfilenames("Aggregator")

        except Exception:
            self.print_log(traceback.format_exc())
            self.alert_and_recover("Problem obtaining logfile name(s)")
            return

        self.complete_state_change(self.name, "booting")

        print "\n%s: BOOT transition complete" % (self.date_and_time()) 


    def do_config(self, config_for_run = None):

        print "\n%s: CONFIG transition underway" % \
            (self.date_and_time())

        os.chdir(self.daqinterface_base_dir)

        if not config_for_run:
            self.config_for_run = self.run_params["config"]
        else:
            self.config_for_run = config_for_run

        try:
            self.config_dirname, self.fhicl_file_path = self.get_config_info()
        except:
            self.revert_failed_transition("calling get_config_info()")
            return

        self.print_log("Config name: %s" % self.config_for_run, 1)
        self.print_log("Selected DAQ comps: %s" %
                       self.daq_comp_list, 1)

        for component, socket in self.daq_comp_list.items():

            self.print_log( make_paragraph( 
                "Searching for the FHiCL document for %s in directory %s given configuration \"%s\"" % \
                (component, self.config_dirname, self.config_for_run)), 2)

            component_fhicl = "the_file_is_not_yet_found"
            
            for dirname, dummy, filenames in os.walk( self.config_dirname ):
                for filename in filenames:
                    if filename == component + "_hw_cfg.fcl":
                        component_fhicl = dirname + "/" + filename
            
            if not os.path.exists(component_fhicl):
                self.revert_state_change(self.name, self.state(self.name))

                msg = "Unable to find FHiCL document for component \"%s\" after searching in directory %s; system remains in the \"%s\" state." % \
                    (component, self.config_dirname, self.state(self.name))

                msg += " Please either select a configuration which contains this component or " + \
                    "terminate and then " + \
                    "boot only with components which exist for configuration \"%s\"." % \
                    (self.config_for_run)

                self.print_log(make_paragraph(msg))

                return

            config_subdirname = os.path.dirname(component_fhicl)
                    
            try:
                for i_proc in range(len(self.procinfos)):

                    if self.procinfos[i_proc].host == socket[0] and \
                            self.procinfos[i_proc].port == socket[1]:
                        self.procinfos[i_proc].ffp = self.fhicl_file_path
                        self.procinfos[i_proc].update_fhicl(component_fhicl)
            except Exception:
                self.print_log(traceback.format_exc())
                self.alert_and_recover("An exception was thrown when creating the process FHiCL documents; see traceback above for more info")
                return
                
        for proc_type in ["EventBuilder", "Aggregator", "DataLogger", "Dispatcher"]:

            rootfile_cntr = 0
            unspecified_aggregator_cntr = 0

            for i_proc in range(len(self.procinfos)):

                if self.procinfos[i_proc].name == proc_type:

                    if proc_type == "EventBuilder":
                        fcl = "%s/EventBuilder1.fcl" % (config_subdirname)
                    elif proc_type == "Aggregator":
                        unspecified_aggregator_cntr += 1
                        if unspecified_aggregator_cntr == 1:
                            fcl = "%s/Aggregator1.fcl" % (config_subdirname)
                        else:
                            fcl = "%s/Aggregator2.fcl" % (config_subdirname)
                    elif proc_type == "DataLogger":
                        fcl = "%s/Aggregator1.fcl" % (config_subdirname)
                    elif proc_type == "Dispatcher":
                        fcl = "%s/Aggregator2.fcl" % (config_subdirname)
                    else:
                        assert False
                        
                    try:
                        self.procinfos[i_proc].ffp = self.fhicl_file_path
                        self.procinfos[i_proc].update_fhicl(fcl)
                    except Exception:
                        self.print_log(traceback.format_exc())
                        self.alert_and_recover("An exception was thrown when creating the process FHiCL documents; see traceback above for more info")
                        return
                        
                    fhicl_before_sub = self.procinfos[i_proc].fhicl_used
                    self.procinfos[i_proc].fhicl_used = re.sub("\.root", "_" + str(rootfile_cntr) + ".root",
                                                               self.procinfos[i_proc].fhicl_used)

                    if self.procinfos[i_proc].fhicl_used != fhicl_before_sub:
                        rootfile_cntr += 1

        for procinfo in self.procinfos:
            assert not procinfo.fhicl is None and not procinfo.fhicl_used is None

        try:
            self.bookkeeping_for_fhicl_documents()
        except Exception:
            self.print_log(traceback.format_exc())
            self.alert_and_recover("An exception was thrown when performing bookkeeping on the process FHiCL documents; see traceback above for more info")
            return
        
        self.tmp_run_record = "/tmp/run_record_attempted_%s" % \
            (os.environ["USER"])
        
        if os.path.exists(self.tmp_run_record):
            shutil.rmtree(self.tmp_run_record)

        try:
            self.save_run_record()            
        except Exception:
            self.print_log(make_paragraph(
                    "WARNING: an exception was thrown when attempting to save the run record. While datataking may be able to proceed, this may also indicate a serious problem"))

        if self.manage_processes:

            try:
                self.do_command("Init")
            except Exception:
                self.print_log(traceback.format_exc())
                self.alert_and_recover("An exception was thrown when attempting to send the \"init\" transition to the artdaq processes; see traceback above for more info")
                return

            try:
                self.launch_art_procs(self.daqinterface_config_file)
            except Exception:
                self.print_log(traceback.format_exc())
                self.print_log(make_paragraph("WARNING: an exception was caught when trying to launch the online monitoring processes; online monitoring won't work though this will not affect actual datataking"))

        self.complete_state_change(self.name, "configuring")

        if self.debug_level >= 1:
            print "To see logfile(s), on %s run \"ls -ltr %s/pmt/%s\"" % \
                (self.pmt_host, self.log_directory,
                 self.log_filename_wildcard)

        print "\n%s: CONFIG transition complete" % (self.date_and_time())

    def do_start_running(self, run_number = None):

        if not run_number:
            self.run_number = self.run_params["run_number"]
        else:
            self.run_number = run_number

        print "\n%s: START transition underway for run %d" % \
            (self.date_and_time(), self.run_number)
        
        if os.path.exists( self.tmp_run_record ):
            run_record_directory = "%s/%s" % \
                (self.record_directory, str(self.run_number))

            cmd = "cp -r %s %s" % (self.tmp_run_record, run_record_directory)
            status = Popen(cmd, shell = True).wait()

            if status != 0:
                self.alert_and_recover("Error in DAQInterface: a nonzero value was returned executing \"%s\"" %
                                       cmd)
                return
        else:
            self.alert_and_recover("Error in DAQInterface: unable to find temporary run records directory %s" % 
                                   self.tmp_run_record)
            return

        try:
            self.put_config_info()
        except Exception:
            self.print_log(traceback.format_exc())
            self.alert_and_recover("An exception was thrown when trying to save configuration info; see traceback above for more info")
            return

        if self.manage_processes:

            try:
                self.do_command("Start")
            except Exception:
                self.print_log(traceback.format_exc())
                self.alert_and_recover("An exception was thrown when attempting to send the \"start\" transition to the artdaq processes; see traceback above for more info")
                return

        self.start_datataking()

        self.save_metadata_value("Start time", \
                                     Popen("date --utc", shell=True, stdout=subprocess.PIPE).stdout.readlines()[0].strip() )

        if self.debug_level >=1:
            print
            print "Run info can be found locally at %s" % \
                (run_record_directory)
            print

        self.complete_state_change(self.name, "starting")
        print "\n%s: START transition complete for run %d" % \
            (self.date_and_time(), self.run_number)

    def do_stop_running(self):

        print "\n%s: STOP transition underway for run %d" % \
            (self.date_and_time(), self.run_number)

        self.save_metadata_value("Stop time", \
                                     Popen("date --utc", shell=True, stdout=subprocess.PIPE).stdout.readlines()[0].strip() )


        self.stop_datataking()

        if self.manage_processes:

            try:
                self.do_command("Stop")
            except Exception:
                self.print_log(traceback.format_exc())
                self.alert_and_recover("An exception was thrown when attempting to send the \"stop\" transition to the artdaq processes; see traceback above for more info")
                return


        self.save_metadata_value("Total events", self.total_events_in_run())
        

        self.complete_state_change(self.name, "stopping")
        print "\n%s: STOP transition complete for run %d" % \
            (self.date_and_time(), self.run_number)

    def do_terminate(self):

        print "\n%s: TERMINATE transition underway" % \
            (self.date_and_time())

        print

        if self.manage_processes:

            for procinfo in self.procinfos:

                try:
                    procinfo.lastreturned = procinfo.server.daq.shutdown()
                except Exception:
                    self.print_log("DAQInterface caught an exception in "
                                   "do_terminate()")
                    self.print_log(traceback.format_exc())

                    self.print_log("%s at %s:%s, returned string is: " % \
                                       (procinfo.name, procinfo.host, procinfo.port))
                    self.print_log(procinfo.lastreturned)

                    self.alert_and_recover("An exception was thrown "
                                           "during the terminate transition")
                    return
                else:
                    if self.debug_level >= 1:
                        print "%s at %s:%s, returned string is: " % \
                            (procinfo.name, procinfo.host, procinfo.port)
                        print procinfo.lastreturned
                        print

            try:
                self.kill_procs()
            except Exception:
                self.print_log("DAQInterface caught an exception in "
                               "do_terminate()")
                self.print_log(traceback.format_exc())
                self.alert_and_recover("An exception was thrown "
                                       "within kill_procs()")
                return

        self.complete_state_change(self.name, "terminating")

        print "\n%s: TERMINATE transition complete" % (self.date_and_time())

        if self.debug_level >= 1:
            print "To see logfile(s), on %s run \"ls -ltr %s/pmt/%s\"" % \
                (self.pmt_host, self.log_directory,
                 self.log_filename_wildcard)

    def do_recover(self):
        print
        print "\n%s: RECOVER transition underway" % \
            (self.date_and_time())

        self.in_recovery = True

        def attempted_stop(self, procinfo):

            if procinfo.name == "DataLogger" or procinfo.name == "Dispatcher":
                greptoken = "AggregatorMain -p " + procinfo.port
            else:
                greptoken = procinfo.name + "Main -p " + procinfo.port

            pid = get_pids(greptoken, procinfo.host)

            if len(pid) == 0:
                if self.debug_level >= 2 or not self.heartbeat_failure:
                    self.print_log(
                        "Didn't find PID for %s at %s:%s" % (procinfo.name, procinfo.host, procinfo.port), 2)
                return

            def send_recover_command(command):
                
                try:
                    if command == "stop":
                        lastreturned=procinfo.server.daq.stop()
                    elif command == "shutdown":
                        lastreturned=procinfo.server.daq.shutdown()
                    else:
                        assert False

                    self.print_log("Called %s on %s at %s:%s without an exception; returned string was \"%s\"" % \
                                       (command, procinfo.name, procinfo.host, procinfo.port, lastreturned), 2)
                except Exception:
                    raise

                if lastreturned == "Success":
                    self.print_log("Successful %s sent to %s at %s:%s" % \
                                       (command, procinfo.name, procinfo.host, procinfo.port), 2)
                else:
                    raise Exception( make_paragraph( \
                            "Attempted %s sent to artdaq process %s " % (command, procinfo.name) + \
                                "at %s:%s during recovery procedure" % (procinfo.host, procinfo.port) + \
                                " returned \"%s\"" % \
                                (lastreturned)))

            try:
                procstatus = procinfo.server.daq.status()
            except Exception:
                self.print_log(make_paragraph("Unable to determine state of artdaq process %s at %s:%s; will not be able to complete its stop-and-shutdown" % \
                                   (procinfo.name, procinfo.host, procinfo.port)))
                return

            if procstatus == "Running":

                try:
                    send_recover_command("stop")
                except Exception:
                    if "ProtocolError" not in traceback.format_exc():
                        print traceback.format_exc()
                    self.print_log( make_paragraph( 
                            "Exception caught during stop transition sent to artdaq process %s " % (procinfo.name) +
                            "at %s:%s during recovery procedure;" % (procinfo.host, procinfo.port) +
                            " it's possible the process no longer existed\n"))
                        
                    return
                    
                try:
                    procstatus = procinfo.server.daq.status()
                except Exception:
                    self.print_log("Unable to determine state of artdaq process %s at %s:%s; will not be able to complete its stop-and-shutdown" % \
                                       (procinfo.name, procinfo.host, procinfo.port))
                    return

            if procstatus == "Ready":

                try:
                    send_recover_command("shutdown")
                except Exception:
                    if "ProtocolError" not in traceback.format_exc():
                        print traceback.format_exc()
                    self.print_log( make_paragraph( 
                            "Exception caught during shutdown transition sent to artdaq process %s " % (procinfo.name) +
                            "at %s:%s during recovery procedure;" % (procinfo.host, procinfo.port) +
                            " it's possible the process no longer existed\n"))
                    return

            return
        

        if self.manage_processes:

            # JCF, Feb-1-2017

            # If an artdaq process has died, the others might follow
            # soon after - if this is the case, then wait a few
            # seconds to give them a chance to die before trying to
            # send them transitions (i.e., so they don't die AFTER a
            # transition is sent, causing more errors)

            if self.heartbeat_failure:
                sleep_on_heartbeat_failure = 0

                if self.debug_level >= 2:
                    self.print_log(
                        make_paragraph(
                            "A process previously was found to be missing; " +
                            "therefore will wait %d seconds before attempting to send the normal transitions as part of recovery" % \
                                (sleep_on_heartbeat_failure)))
                sleep(sleep_on_heartbeat_failure)  


            for name in ["BoardReader", "EventBuilder", "Aggregator", "DataLogger", "Dispatcher"]:

                threads = []
                priorities_used = {}

                for procinfo in self.procinfos:
                    if name in procinfo.name:
                        priorities_used[ procinfo.priority ] = "We only care about the key in the dict"

                for priority in sorted(priorities_used.iterkeys(), reverse = True):
                    for procinfo in self.procinfos:
                        if name in procinfo.name and priority == procinfo.priority:
                            t = Thread(target=attempted_stop, args=(self, procinfo))
                            threads.append(t)
                            t.start()

                    for thread in threads:
                        thread.join()

            try:
                self.kill_procs()
            except Exception:
                self.print_log(traceback.format_exc())
                self.print_log(make_paragraph("An exception was thrown "
                                       "within kill_procs(); artdaq processes may not all have been killed"))

        self.in_recovery = False

        self.complete_state_change(self.name, "recovering")

        print "\n%s: RECOVER transition complete" % (self.date_and_time())

    # Override of the parent class Component's runner function. As of
    # 5/30/14, called every 1s by control.py

    def runner(self):
        """
        Component "ops" loop.  Called at threading hearbeat frequency,
        currently 1/sec.
        """

        try:

            if self.in_recovery:
                pass

            elif self.__do_boot:
                self.__do_boot = False
                self.do_boot()

            elif self.__do_shutdown:
                self.__do_shutdown = False
                self.do_command("Shutdown")


            elif self.__do_config:
                self.__do_config = False
                self.do_config()


            elif self.__do_recover:
                self.__do_recover = False
                self.do_recover()


            elif self.__do_start_running:
                self.__do_start_running = False
                self.do_start_running()


            elif self.__do_stop_running:
                self.__do_stop_running = False
                self.do_stop_running()


            elif self.__do_terminate:
                self.__do_terminate = False
                self.do_terminate()


            elif self.__do_pause_running:
                self.__do_pause_running = False
                self.do_command("Pause")


            elif self.__do_resume_running:
                self.__do_resume_running = False
                self.do_command("Resume")


            elif self.manage_processes and self.state(self.name) != "stopped" and \
                    self.state(self.name) != "booting" and self.state(self.name) != "terminating":
                self.check_proc_heartbeats()
                self.check_proc_exceptions()

        except Exception:
            self.in_recovery = True
            self.alert_and_recover(traceback.format_exc())
            self.in_recovery = False


def get_args():  # no-coverage
    parser = argparse.ArgumentParser(
        description="DAQInterface")
    parser.add_argument("-n", "--name", type=str, dest='name',
                        default="daqint", help="Component name")
    parser.add_argument("-r", "--rpc-port", type=int, dest='rpc_port',
                        default=5570, help="RPC port")
    parser.add_argument("-H", "--rpc-host", type=str, dest='rpc_host',
                        default='localhost', help="This hostname/IP addr")
    parser.add_argument("-c", "--control-host", type=str, dest='control_host',
                        default='localhost', help="Control host")
    return parser.parse_args()


def main():  # no-coverage

    greptoken = "python.*daqinterface.py"
    pids = get_pids(greptoken)

    if len(pids) > 1:
        print make_paragraph("Won't launch DAQInterface; it appears an instance is already running on this host according to this command:" )
        print "\nps aux | grep \"%s\" | grep -v grep\n" % (greptoken)
        return

    if not os.path.exists("./bin"):
        print make_paragraph("Won't launch DAQInterface; you need to be in the base directory of this package")

    if "DAQINTLOGDIR" not in os.environ.keys():
        print make_paragraph("Won't launch DAQInterface; you first need to run \"source source_me\" from the base directory of this package")
        return

    args = get_args()

    with DAQInterface(logpath=os.path.join(os.environ["HOME"], ".lbnedaqint.log"),
                      **vars(args)):
        try:
            while True:
                sleep(100)
        except: KeyboardInterrupt

if __name__ == "__main__":
    main()
