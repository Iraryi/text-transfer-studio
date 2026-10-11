"""Real Tk widget/controller tests; isolated temp exports, neutral sample only."""
from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PIL import Image
from app import Editor
import model as m


class UiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.cleanup_root)
        self.editor=Editor(self.root,True);self.root.update()

    def cleanup_root(self):
        if self.editor.pending:self.root.after_cancel(self.editor.pending)
        self.root.destroy()

    def test_edit_and_transfer_then_undo_redo(self):
        e=self.editor;e.text.delete('1.0','end');e.text.insert('1.0','迁移测试');e.vars['size'].set('70');e.apply()
        old=e.layer();self.assertEqual(old.text,'迁移测试');self.assertEqual(old.size,70)
        e.transfer(0);self.assertEqual(e.active,1);self.assertEqual(e.layer().text,'迁移测试')
        self.assertNotEqual(e.layer().id,old.id);self.assertEqual(len(e.docs[1].layers),3)
        e.undo();self.assertEqual(len(e.docs[1].layers),2);e.undo(True);self.assertEqual(len(e.docs[1].layers),3)

    def test_invalid_style_blocks_export_dialog(self):
        e=self.editor;e.vars['size'].set('not a number')
        with patch('app.messagebox.showerror') as error,patch('app.filedialog.asksaveasfilename') as dialog:
            e.export(0);error.assert_called_once();dialog.assert_not_called()

    def test_transfer_all_layers_one_click(self):
        e=self.editor;e.transfer_scope.set('全部文字层');e.transfer(0)
        self.assertEqual(len(e.docs[1].layers),4)
        self.assertEqual([x.text for x in e.docs[1].layers[2:]],[x.text for x in e.docs[0].layers])
        self.assertEqual(len({x.id for doc in e.docs for x in doc.layers}),6)
        e.undo();self.assertEqual(len(e.docs[1].layers),2)

    def test_export_rejects_original_target(self):
        e=self.editor;path=self.directory/'original.png';e.docs[0].image.save(path);before=path.read_bytes()
        e.docs[0].source_path=str(path.resolve())
        with patch('app.filedialog.asksaveasfilename',return_value=str(path)),patch('app.messagebox.showerror') as error:
            e.export(0);error.assert_called_once()
        self.assertEqual(path.read_bytes(),before)

    def test_export_and_project_save_reload(self):
        e=self.editor;png=self.directory/'export.png';project=self.directory/'demo.textshift'
        with patch('app.filedialog.asksaveasfilename',return_value=str(png)):e.export(0,True)
        with Image.open(png) as image:self.assertEqual(image.getpixel((0,0)),(0,0,0,0))
        with patch('app.filedialog.asksaveasfilename',return_value=str(project)):e.save()
        self.assertFalse(e.dirty);e.docs=[m.blank(),m.blank()]
        with patch('app.filedialog.askopenfilename',return_value=str(project)):e.open_project()
        self.assertEqual(len(e.docs[0].layers),2);self.assertEqual(e.docs[0].layers[0].text,'把文字，放到合适的位置。')

    def test_drag_and_corner_transform(self):
        e=self.editor;e.geometry[0]=(0,0,1);layer=e.layer();doc=e.docs[0]
        x,y=layer.x*doc.image.width,layer.y*doc.image.height
        e.press(0,SimpleNamespace(x=x,y=y));e.motion(0,SimpleNamespace(x=x+100,y=y+30));e.release()
        self.assertAlmostEqual(e.layer().x,.5+100/doc.image.width)
        self.assertAlmostEqual(e.layer().y,.4+30/doc.image.height)
        e.mode.set('四角变形');a,b=m.corners(e.layer(),doc.image.size)[0]
        e.press(0,SimpleNamespace(x=a,y=b));e.motion(0,SimpleNamespace(x=a+20,y=b+10));e.release()
        self.assertIsNotNone(e.layer().quad);e.layer().validate()
        e.reset_quad();self.assertIsNone(e.layer().quad)

    def test_duplicate_visibility_and_order(self):
        e=self.editor;original=e.layer().id;e.duplicate();self.assertNotEqual(e.layer().id,original)
        e.visible.set(False);e.apply();self.assertFalse(e.layer().visible)
        selected=e.layer().id;e.reorder(-1);self.assertEqual(e.docs[0].layers[1].id,selected)
        e.delete();self.assertEqual(len(e.docs[0].layers),2)


if __name__=='__main__':unittest.main(verbosity=2)
