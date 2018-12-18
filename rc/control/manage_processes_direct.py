
import random
import string
import os
import subprocess
from subprocess import Popen
import socket
from time import sleep
import re
import sys

sys.path.append( os.environ["ARTDAQ_DAQINTERFACE_DIR"] )

from rc.control.utilities import get_pids
from rc.control.utilities import bash_unsetup_command
from rc.control.utilities import date_and_time
from rc.control.utilities import construct_checked_command
from rc.control.utilities import obtain_messagefacility_fhicl
from rc.control.utilities import make_paragraph
from rc.control.deepsuppression import deepsuppression


def bootfile_name_to_execname(bootfile_name):

    if "BoardReader" in bootfile_name:
        execname = "boardreader"
    elif "EventBuilder" in bootfile_name:
        execname = "eventbuilder"
    elif "DataLogger" in bootfile_name:
        execname = "datalogger"
    elif "Dispatcher" in bootfile_name:
        execname = "dispatcher"
    elif "RoutingMaster" in bootfile_name:
        execname = "routing_master"
    else:
        assert False

    return execname
    

def launch_procs_base(self):

    if self.have_artdaq_mfextensions():
        messagefacility_fhicl_filename = obtain_messagefacility_fhicl()

    launch_commands_to_run_on_host = {}
    launch_commands_to_run_on_host_background = {}  # Need to run artdaq processes in the background so they're persistent outside of this function's Popen calls

    for procinfo in self.procinfos:

        if not procinfo.host in launch_commands_to_run_on_host:
            launch_commands_to_run_on_host[ procinfo.host ] = []
            launch_commands_to_run_on_host[ procinfo.host ].append(". %s/setup" % self.productsdir)  
            launch_commands_to_run_on_host[ procinfo.host ].append( bash_unsetup_command )
            launch_commands_to_run_on_host[ procinfo.host ].append("source " + self.daq_setup_script )
            launch_commands_to_run_on_host[ procinfo.host ].append("export ARTDAQ_LOG_ROOT=%s" % (self.log_directory))
            launch_commands_to_run_on_host[ procinfo.host ].append("export ARTDAQ_LOG_FHICL=%s" % (messagefacility_fhicl_filename))
            launch_commands_to_run_on_host[ procinfo.host ].append("which boardreader") # Assume if this works, eventbuilder, etc. are also there

            launch_commands_to_run_on_host_background[ procinfo.host ] = []

        launch_commands_to_run_on_host_background[ procinfo.host ].append( " %s -c \"id: %s commanderPluginType: xmlrpc rank: %s application_name: %s partition_number: %s\" & " % \
                                                   (bootfile_name_to_execname(procinfo.name), procinfo.port, procinfo.rank, procinfo.label, 
                                                    os.environ["DAQINTERFACE_PARTITION_NUMBER"]))
    
    for host in launch_commands_to_run_on_host:
        
        # Before we try launching the processes, let's make sure there
        # aren't any pre-existing processes listening on the same
        # ports

        grepped_lines = []
        preexisting_pids = get_pids("\|".join([ "%s.*id:\s\+%s" % 
                                                (bootfile_name_to_execname(procinfo.name), procinfo.port) for \
                                                procinfo in self.procinfos if procinfo.host == host ]),
                                    host,
                                    grepped_lines)
        if len(preexisting_pids) > 0:
            self.print_log("e", make_paragraph("On host %s, found artdaq process(es) already existing which use the ports DAQInterface was going to use; this may be the result of an improper cleanup from a prior run: " % (host)))
            self.print_log("e", "\n" + "\n".join(grepped_lines))
            self.print_log("i", "...note that the process(es) may get automatically cleaned up during DAQInterface recovery\n")
            raise Exception("DAQInterface found previously-existing artdaq processes using desired ports; see error message above for details")
        

        launchcmd = construct_checked_command( launch_commands_to_run_on_host[ host ] )
        launchcmd += "; "
        launchcmd += " ".join(launch_commands_to_run_on_host_background[ host ])  # Each command already terminated by ampersand

        if host != os.environ["HOSTNAME"] and host != "localhost":
            launchcmd = "ssh -f " + host + " '" + launchcmd + "'"

        self.print_log("d", "PROCESS LAUNCH COMMANDS TO EXECUTE ON %s: %s%s\n" % (host, "\n".join( launch_commands_to_run_on_host[ host ] ), "\n".join( launch_commands_to_run_on_host_background[ host ])), 2)
        
        with deepsuppression(self.debug_level < 4):
            status = Popen(launchcmd, shell=True).wait()

        if status != 0:   
            raise Exception("Status error raised by running the following command on %s: \"\n%s\n\". If logfiles exist, please check them for more information. Also try running the commands interactively in a new terminal after logging into %s" %
                            (host, launchcmd, host))

    return


def kill_procs_base(self):

    for procinfo in self.procinfos:

        pid = get_pid_for_process(procinfo)

        if pid is not None:
            cmd = "kill " + pid

            if procinfo.host != "localhost" and procinfo.host != os.environ["HOSTNAME"]:
                cmd = "ssh -f " + procinfo.host + " '" + cmd + "'"

            self.print_log("d", "Killing %s process on %s, pid == %s" % (procinfo.label, procinfo.host, pid), 2)
            Popen(cmd, shell=True, stdout=subprocess.PIPE,
                  stderr=subprocess.STDOUT)
        else:
            self.print_log("d", "No process for %s found; will not issue a kill command" % (procinfo.label), 2)

    # Check that they were actually killed

    sleep(1)

    for procinfo in self.procinfos:
        pid = get_pid_for_process(procinfo)

        if pid is not None:
            self.print_log("w", "Appeared to be unable to kill %s on %s during cleanup" % \
                               (procinfo.label, procinfo.host))

    for host in set([procinfo.host for procinfo in self.procinfos]):
        art_pids = get_pids("art -c .*partition_%s" % os.environ["DAQINTERFACE_PARTITION_NUMBER"], host)

        if len(art_pids) > 0:
            cmd = "kill -9 %s" % (" ".join( art_pids ) )   # JCF, Dec-8-2018: the "-9" is apparently needed...
            if host != "localhost" and host != os.environ["HOSTNAME"]:
                cmd = "ssh -f " + host + " '" + cmd + "'"
            self.print_log("d", "Executing \"%s\" on %s" % (cmd, host), 2)
            Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).wait()

        art_pids = get_pids("art -c .*partition_%s" % os.environ["DAQINTERFACE_PARTITION_NUMBER"], host)
        if len(art_pids) > 0:
            self.print_log("w", "Unable to kill at least one of the artdaq-related art processes on %s (pid(s) %s still exist)" % (host, " ".join(art_pids)))




    self.procinfos = []

    return

def softlink_process_manager_logfiles_base(self):
    return

def find_process_manager_variable_base(self, line):
    return False

def set_process_manager_default_variables_base(self):
    pass # There ARE no persistent variables specific to direct process management

def reset_process_manager_variables_base(self):
    pass

def get_process_manager_log_filenames_base(self):
    return []

def process_manager_cleanup_base(self):
    pass

def get_pid_for_process(procinfo):
    greptoken = bootfile_name_to_execname(procinfo.name) + " -c .*" + procinfo.port + ".*"

    grepped_lines = []
    pids = get_pids(greptoken, procinfo.host, grepped_lines)

    if len(pids) == 1:    
        return pids[0]
    elif len(pids) == 0:
        return None
    else:
        for grepped_line in grepped_lines:
            print grepped_line

        "Appear to have duplicate processes for %s on %s, pids: %s" % (procinfo.label, procinfo.host, " ".join( pids ))
        #assert False, "Unexpected error grepping for \"%s\" on %s" % (greptoken, procinfo.host)

def get_related_pids_for_process(procinfo):
    related_pids = []

    netstat_cmd = "netstat -alpn | grep %s" % (procinfo.port)

    if procinfo.host != "localhost" and procinfo.host != os.environ["HOSTNAME"]:
        netstat_cmd = "ssh -f %s '%s'" % (procinfo.host, netstat_cmd)

    proc = Popen(netstat_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    for procline in proc.stdout.readlines():
        res = re.search(r"([0-9]+)/(.*)", procline.split()[-1])
        if res:
            pid = res.group(1)
            pname = res.group(2)
            if "python" not in pname:  # Don't want DAQInterface to kill itself off...
                related_pids.append( res.group(1) )
    return set(related_pids)



# check_proc_heartbeats_base() will check that the expected artdaq
# processes are up and running

def check_proc_heartbeats_base(self, requireSuccess=True):

    is_all_ok = True

    procinfos_to_remove = []

    for procinfo in self.procinfos:

        if get_pid_for_process(procinfo) is None:
            is_all_ok = False

            if requireSuccess:
                self.print_log("e", "%s: Appear to have lost process with label %s on host %s" % (date_and_time(), procinfo.label, procinfo.host))
                procinfos_to_remove.append( procinfo )

                # Will need to perform some cleanup (clogged ports, zombie art processes, etc.)
                ssh_mopup_ok = True  
                related_process_mopup_ok = True

                # Need to deal with the lingering ssh command if the lost process is on a remote host
                if procinfo.host != "localhost" and procinfo.host != os.environ["HOSTNAME"]:
                    
                    # Mopup the ssh call on this side
                    ssh_grepstring = "ssh.*%s.*%s -c.*%s" % (procinfo.host, bootfile_name_to_execname(procinfo.name),
                                                            procinfo.label) 
                    pids = get_pids(ssh_grepstring)

                    if len(pids) == 1:
                        Popen("kill %s" % (pids[0]), shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE).wait()
                        pids = get_pids(ssh_grepstring)
                        if len(pids) == 1:
                            ssh_mopup_ok = False
                    else:
                        ssh_mopup_ok = False

                # And take out the process(es) associated with the artdaq process via its listening port (e.g., the art processes)

                Popen("ssh -f %s 'kill %s'" % (procinfo.host, " ".join(get_related_pids_for_process(procinfo))), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).wait()

                unkilled_related_pids = get_related_pids_for_process(procinfo)
                if len(unkilled_related_pids) == 0:
                    related_process_mopup_ok = True
                else:
                    self.print_log("w", make_paragraph("Warning: unable to normally kill process(es) associated with now-deceased artdaq process %s; on %s the following pid(s) remain: %s. Will now resort to kill -9 on these processes." % (procinfo.label, procinfo.host, " ".join(unkilled_related_pids))))
                    Popen("ssh -f %s 'kill -9 %s'" % (procinfo.host, " ".join(unkilled_related_pids)), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).wait()
                    related_process_mopup_ok = False

                if not ssh_mopup_ok:
                    self.print_log("w", make_paragraph("There was a problem killing the ssh process to %s related to the deceased artdaq process %s at %s:%s; there *may* be issues with the next run using that host and port as a result" % (procinfo.host, procinfo.label, procinfo.host, procinfo.port)))
                if not related_process_mopup_ok:
                    self.print_log("w", make_paragraph("At least some of the processes on %s related to deceased artdaq process %s at %s:%s (e.g. art processes) had to be forcibly killed; there *may* be issues with the next run using that host and port as a result" % (procinfo.host, procinfo.label, procinfo.host, procinfo.port)))
                        
    if not is_all_ok and requireSuccess:
        for procinfo in procinfos_to_remove:
            self.procinfos.remove( procinfo )

        print
        self.print_log("i", "Processes remaining:\n%s" % ("\n".join( [procinfo.label for procinfo in self.procinfos])))
        return

    return is_all_ok


def main():
    
    # JCF, Dec-7-2018

    # This is a toy version of the true Procinfo class defined within
    # the DAQInterface class, meant to be used just for testing this
    # module

    class Procinfo(object):
        def __init__(self, name, rank, host, port, label):
            self.name = name
            self.rank = rank
            self.port = port
            self.host = host
            self.label = label


    launch_procs_test = True

    if launch_procs_test:

        class MockDAQInterface:
            debug_level = 3
            productsdir = "/mu2e/ups"
            daq_setup_script = "/home/jcfree/artdaq-demo_multiple_fragments_per_boardreader/setupARTDAQDEMO"

            procinfos = []
            procinfos.append( Procinfo("BoardReader", "0", "localhost", "10100", "MockBoardReader") )
            procinfos.append( Procinfo("EventBuilder", "1", "localhost", "10101", "MockEventBuilder") )

            def print_log(self, ignore, string_to_print, ignore2):
                print string_to_print
            

        launch_procs_base( MockDAQInterface() )

if __name__ == "__main__":
    main()

                        
