
"""
Test our tools for recovering the content of damaged journals
"""

import contextlib
import logging
from textwrap import dedent
import time
from teuthology.orchestra.run import CommandFailedError
from tasks.cephfs.filesystem import Filesystem, ObjectNotFound, ROOT_INO
from tasks.cephfs.cephfs_test_case import CephFSTestCase, run_tests


log = logging.getLogger(__name__)


class TestJournalRepair(CephFSTestCase):
    def test_inject_to_empty(self):
        """
        That when some dentries in the journal but nothing is in
        the backing store, we correctly populate the backing store
        from the journalled dentries.
        """

        # Inject metadata operations
        self.mount_a.run_shell(["touch", "rootfile"])
        self.mount_a.run_shell(["mkdir", "subdir"])
        self.mount_a.run_shell(["touch", "subdir/subdirfile"])
        rootfile_ino = self.mount_a.path_to_ino("rootfile")
        subdir_ino = self.mount_a.path_to_ino("subdir")
        subdirfile_ino = self.mount_a.path_to_ino("subdir/subdirfile")
        self.mount_a.umount_wait()

        # Stop the MDS
        self.fs.mds_stop()
        self.fs.mds_fail()

        # Now, the journal should contain the operations, but the backing
        # store shouldn't
        with self.assertRaises(ObjectNotFound):
            self.fs.list_dirfrag(subdir_ino)
        self.assertEqual(self.fs.list_dirfrag(ROOT_INO), [])

        # Execute the dentry recovery, this should populate the backing store
        self.fs.journal_tool(['event', 'recover_dentries', 'list'])

        # Dentries in ROOT_INO are present
        self.assertEqual(self.fs.list_dirfrag(ROOT_INO), ['rootfile_head', 'subdir_head'])
        self.assertEqual(self.fs.list_dirfrag(subdir_ino), ['subdirfile_head'])

        # Now check the MDS can read what we wrote: truncate the journal
        # and start the mds.
        self.fs.journal_tool(['journal', 'reset'])
        self.fs.mds_restart()
        self.fs.wait_for_daemons()

        # List files
        self.mount_a.mount()
        self.mount_a.wait_until_mounted()
        proc = self.mount_a.run_shell(['ls', '-R'])
        self.assertEqual(proc.stdout.getvalue().strip(),
                         dedent("""
                         .:
                         rootfile
                         subdir

                         ./subdir:
                         subdirfile
                         """).strip())

        # Check the correct inos were preserved by path
        self.assertEqual(rootfile_ino, self.mount_a.path_to_ino("rootfile"))
        self.assertEqual(subdir_ino, self.mount_a.path_to_ino("subdir"))
        self.assertEqual(subdirfile_ino, self.mount_a.path_to_ino("subdir/subdirfile"))

        # Create a new file, ensure it is not issued the same ino as one of the
        # recovered ones
        self.mount_a.run_shell(["touch", "afterwards"])
        new_ino = self.mount_a.path_to_ino("afterwards")
        self.assertNotIn(new_ino, [rootfile_ino, subdir_ino, subdirfile_ino])

    def test_reset(self):
        """
        That after forcibly modifying the backing store, we can get back into
        a good state by resetting the MDSMap.

        The scenario is that we have two active MDSs, and we lose the journals.  Once
        we have completely lost confidence in the integrity of the metadata, we want to
        return the system to a single-MDS state to go into a scrub to recover what we
        can.
        """

        # Set max_mds to 2
        self.fs.mon_manager.raw_cluster_cmd_result('mds', 'set', "max_mds", "2")

        # See that we have two active MDSs
        self.wait_until_equal(lambda: len(self.fs.get_active_names()), 2, 30,
                              reject_fn=lambda v: v > 2 or v < 1)
        active_mds_names = self.fs.get_active_names()

        # Do a bunch of I/O such that at least some will hit the second MDS: create
        # lots of directories so that the balancer should find it easy to make a decision
        # to allocate some of them to the second mds.
        spammers = []
        for n in range(0, 16):
            dir_name = "spam_{0}".format(n)
            spammers.append(self.mount_a.spam_dir_background(dir_name))

        def subtrees_assigned():
            got_subtrees = self.fs.mds_asok(["get", "subtrees"], mds_id=active_mds_names[0])
            rank_1_count = len([s for s in got_subtrees if s['auth_first'] == 1])

            # Greater than 1, because there is typically 1 for ~mds1, and once it
            # has been assigned something in addition to that it means it has been
            # assigned a "real" subtree.
            return rank_1_count > 1

        # We are waiting for the MDS to respond to hot directories, which
        # is not guaranteed to happen at a particular time, so a lengthy timeout here.
        self.wait_until_true(subtrees_assigned, 600)

        # Flush the journals so that we have some backing store data
        # belonging to one MDS, and some to the other MDS.
        for mds_name in active_mds_names:
            self.fs.mds_asok(["flush", "journal"], mds_name)

        # Stop (hard) the second MDS daemon
        self.fs.mds_stop(active_mds_names[1])

        # Wipe out the tables for MDS rank 1 so that it is broken and can't start
        # (this is the simulated failure that we will demonstrate that the disaster
        #  recovery tools can get us back from)
        self.fs.erase_metadata_objects(prefix="mds1_")

        # Try to access files from the client
        blocked_ls = self.mount_a.run_shell(["ls", "-R"], wait=False)

        # Check that this "ls -R" blocked rather than completing: indicates
        # it got stuck trying to access subtrees which were on the now-dead MDS.
        log.info("Sleeping to check ls is blocked...")
        time.sleep(60)
        self.assertFalse(blocked_ls.finished)

        # See that the blocks walking the full tree because some
        # parts are assigned to the now-dead MDS

        # This mount is now useless because it will depend on MDS rank 1, and MDS rank 1
        # is not coming back.  Kill it.
        log.info("Killing mount, it's blocked on the MDS we killed")
        self.mount_a.kill()
        self.mount_a.kill_cleanup()
        try:
            # Now that the mount is dead, the ls -R should error out.
            blocked_ls.wait()
        except CommandFailedError:
            pass

        log.info("Terminating spammer processes...")
        for spammer_proc in spammers:
            spammer_proc.stdin.close()
            try:
                spammer_proc.wait()
            except CommandFailedError:
                pass

        # See that the second MDS will crash when it starts and tries to
        # acquire rank 0
        self.fs.mds_restart(active_mds_names[1])
        crasher = self.fs.mds_daemons[active_mds_names[1]].proc

        try:
            crasher.wait()
        except CommandFailedError as e:
            log.info("MDS '{0}' crashed with status {1} as expected".format(active_mds_names[1], e.exitstatus))
            self.fs.mds_daemons[active_mds_names[1]].proc = None
        else:
            raise RuntimeError("MDS daemon '{0}' did not crash as expected".format(active_mds_names[1]))

        # Now it's crashed, let the MDSMonitor know that it's not coming back
        self.fs.mds_fail(active_mds_names[1])

        # Now give up and go through a disaster recovery procedure
        self.fs.mds_stop(active_mds_names[0])
        self.fs.mds_fail(active_mds_names[0])
        self.fs.journal_tool(["event", "recover_dentries", "summary"], rank=0)
        self.fs.journal_tool(["event", "recover_dentries", "summary"], rank=1)
        self.fs.table_tool(["all", "reset", "session"], rank=0)
        self.fs.journal_tool(["journal", "reset"], rank=0)
        self.fs.erase_mds_objects(1)
        self.fs.mon_remote.run(args=['sudo', 'ceph', 'fs', 'reset', 'default', '--yes-i-really-mean-it'])

        # Bring an MDS back online, mount a client, and see that we can walk the full
        # filesystem tree again
        self.fs.mds_restart(active_mds_names[0])
        self.wait_until_equal(lambda: self.fs.get_active_names(), [active_mds_names[0]], 30,
                              reject_fn=lambda v: len(v) > 1)
        self.mount_a.mount()
        self.mount_a.run_shell(["ls", "-R"], wait=True)


@contextlib.contextmanager
def task(ctx, config):
    fs = Filesystem(ctx)

    # Pick out the clients we will use from the configuration
    # =======================================================
    if len(ctx.mounts) < 1:
        raise RuntimeError("Need at least one clients")
    mount_a = ctx.mounts.values()[0]

    # Stash references on ctx so that we can easily debug in interactive mode
    # =======================================================================
    ctx.filesystem = fs
    ctx.mount_a = mount_a

    run_tests(ctx, config, TestJournalRepair, {
        'fs': fs,
        'mount_a': mount_a
    })

    # Continue to any downstream tasks
    # ================================
    yield
