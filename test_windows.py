"""Real Tk modal lifecycle regression tests. No user project or credentials loaded."""
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch
from app import Editor


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw();self.editor=Editor(self.root)
        self.root.geometry('1100x740');self.root.deiconify();self.root.update()
        self.editor.workflow(0);self.flow=self.editor.workflow_window;self.root.update()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.editor.modals.shutdown()
        self.flow.closed=True
        if self.flow.timer:self.flow.win.after_cancel(self.flow.timer)
        if self.editor.pending:self.root.after_cancel(self.editor.pending)
        self.root.destroy()

    def test_settings_has_direct_root_owner_and_single_instance(self):
        dialog=self.flow.settings();self.root.update()
        self.assertEqual(str(dialog.transient()),str(self.root))
        self.assertEqual(str(self.flow.win.transient()),str(self.root))
        self.assertIs(self.flow.settings(),dialog)
        self.assertEqual(len(self.editor.modals.stack),2)

    def test_minimize_restore_recovers_dialog_and_draft(self):
        dialog=self.flow.settings();self.root.update()
        body=dialog.winfo_children()[0];entries=[x for x in body.winfo_children() if isinstance(x,ttk.Entry)]
        entries[1].insert(0,'FAKE-DRAFT-NOT-A-KEY')
        self.root.iconify();self.root.update()
        self.assertIsNone(self.root.grab_current())
        self.root.deiconify();self.root.update()
        self.assertTrue(self.flow.win.winfo_viewable());self.assertTrue(dialog.winfo_viewable())
        self.assertIs(self.root.grab_current(),dialog)
        self.assertEqual(entries[1].get(),'FAKE-DRAFT-NOT-A-KEY')
        self.assertEqual(self.editor.ai_config.key,'')

    def test_restore_never_forces_focus_when_another_app_is_foreground(self):
        dialog=self.flow.settings();self.root.update()
        with patch.object(self.editor.modals,'foreground_is_ours',return_value=False),patch.object(dialog,'focus_set') as focus,patch.object(dialog,'lift') as lift:
            self.editor.modals.schedule(True);self.root.update();focus.assert_not_called();lift.assert_not_called()

    def test_parent_activation_restores_hidden_settings(self):
        dialog=self.flow.settings();self.root.update();dialog.withdraw();self.root.update()
        self.assertIsNone(self.root.grab_current())
        self.editor.modals.activated(self.root);self.root.update()
        self.assertTrue(dialog.winfo_viewable());self.assertIs(self.root.grab_current(),dialog)

    def test_settings_close_restores_workflow_grab_without_saving(self):
        dialog=self.flow.settings();self.root.update()
        self.root.tk.call(dialog.protocol('WM_DELETE_WINDOW'));self.root.update()
        self.assertFalse(dialog.winfo_exists());self.assertIs(self.root.grab_current(),self.flow.win)
        self.assertIsNone(self.flow.ai_dialog);self.assertFalse(self.editor.ai_config.enabled)


if __name__=='__main__':unittest.main(verbosity=2)
