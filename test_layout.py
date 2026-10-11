import unittest
from unittest.mock import patch
from types import SimpleNamespace
import model as m
import layout
import style_match
import imaging
from PIL import Image
import test_ui


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.size=(1000,500)
        self.layer=m.Layer(text='TEST',x=.25,y=.3,quad=[(.2,.2),(.3,.2),(.3,.4),(.2,.4)])

    def test_translation_and_original_unchanged(self):
        moved=layout.translated(self.layer,self.size,1,-10)
        self.assertAlmostEqual(moved.quad[0][0],.201)
        self.assertAlmostEqual(moved.quad[0][1],.18)
        self.assertEqual(self.layer.quad[0],(.2,.2))

    def test_center_axes_preserve_shape(self):
        for axis in ('x','y','both'):
            new=layout.centered(self.layer,self.size,axis)
            l,t,r,b=layout.bounds(new,self.size)
            self.assertAlmostEqual(r-l,100);self.assertAlmostEqual(b-t,100)
            if axis in ('x','both'):self.assertAlmostEqual((l+r)/2,500)
            if axis in ('y','both'):self.assertAlmostEqual((t+b)/2,250)
        text=layout.centered(self.layer,self.size,'text')
        self.assertEqual(text.quad,self.layer.quad)

    def test_center_snap_and_out_of_range(self):
        for shift,expected in [(247,500),(240,490)]:
            near=layout.translated(self.layer,self.size,shift,0)
            new,guides=layout.snap(near,self.size,tolerance=6)
            l,t,r,b=layout.bounds(new,self.size)
            self.assertAlmostEqual((l+r)/2,expected)
            self.assertEqual(bool(guides),shift==247)

    def test_parallel_edge_and_hidden_target(self):
        other=m.Layer(quad=[(.4,.1),(.6,.1),(.6,.6),(.4,.6)])
        candidate=layout.translated(self.layer,self.size,96,0)
        new,guides=layout.snap(candidate,self.size,[other])
        self.assertAlmostEqual(layout.bounds(new,self.size)[2],400)
        other.visible=False
        new,guides=layout.snap(candidate,self.size,[other])
        self.assertAlmostEqual(layout.bounds(new,self.size)[2],396)

    def test_mirrored_center(self):
        other=m.Layer(quad=[(.15,.02),(.25,.02),(.25,.1),(.15,.1)])
        candidate=layout.translated(self.layer,self.size,547,0)
        new,guides=layout.snap(candidate,self.size,[other])
        l,t,r,b=layout.bounds(new,self.size)
        self.assertAlmostEqual((l+r)/2,800)

    def test_slanted_parallel_edge(self):
        other=m.Layer(quad=[(.4,.1),(.6,.2),(.6,.5),(.4,.4)])
        candidate=m.Layer(quad=[(.42,.114),(.58,.194),(.58,.27),(.42,.19)])
        new,guides=layout.snap(candidate,self.size,[other],tolerance=5)
        # Top edge is parallel to the target's top edge and lies on it after snapping.
        p=m.corners(new,self.size)[0]
        self.assertAlmostEqual(p[1]-.25*p[0],-50,places=5)


class StylePolicyTests(unittest.TestCase):
    def test_disabled_ignores_existing_estimate(self):
        row={'id':'r','source':'TEST','target':'译文','score':1,
             'box':[[.1,.1],[.8,.1],[.8,.4],[.1,.4]],
             'style':{'font':m.default_font(),'color':'#ff0000','outline':5,'size':60,'match_score':.99,'font_margin':.2}}
        doc=m.Document(Image.new('RGBA',(600,300)))
        default=imaging.layers_from_records(doc,[row])[0]
        enabled=imaging.layers_from_records(doc,[row],auto_style=True)[0]
        self.assertEqual(default.outline,0);self.assertNotEqual(default.color,'#ff0000')
        self.assertEqual(enabled.outline,5);self.assertEqual(enabled.color,'#ff0000')

    def test_sensitivity_and_margin(self):
        weak={'match_score':.6,'font_margin':.01}
        self.assertFalse(style_match.accepted_style(weak,True,0))
        self.assertTrue(style_match.accepted_style(weak,True,100))
        ambiguous={'match_score':.95,'font_margin':.001}
        self.assertFalse(style_match.accepted_style(ambiguous,True,0))
        with self.assertRaises(ValueError):style_match.accepted_style(weak,True,-1)


class InteractionTests(unittest.TestCase):
    setUp=test_ui.UiTests.setUp
    cleanup_root=test_ui.UiTests.cleanup_root
    def test_defaults_and_nudges(self):
        e=self.editor
        self.assertFalse(e.auto_style.get());self.assertEqual(e.style_sensitivity.get(),0)
        e.snap_enabled.set(True);old=e.layer().x
        e.nudge_event(SimpleNamespace(widget=e.canvases[0],keysym='Right',state=0))
        self.assertAlmostEqual(e.layer().x,old+1/e.docs[0].image.width)
        e.nudge_event(SimpleNamespace(widget=e.canvases[0],keysym='Left',state=1))
        self.assertAlmostEqual(e.layer().x,old-9/e.docs[0].image.width)
        e.undo();self.assertAlmostEqual(e.layer().x,old+1/e.docs[0].image.width)

    def test_input_arrows_do_not_move_layer(self):
        e=self.editor;old=e.layer().x
        self.assertIsNone(e.nudge_event(SimpleNamespace(widget=e.text,keysym='Right',state=0)))
        self.assertEqual(e.layer().x,old)

    def test_center_ui_and_undo(self):
        e=self.editor;old=e.layer().y
        e.center('y');self.assertEqual(e.layer().y,.5)
        e.undo();self.assertEqual(e.layer().y,old)

    def test_mcp_config_dialog_closes_and_releases_modal(self):
        e=self.editor;errors=[]
        self.root.report_callback_exception=lambda *args:errors.append(args)
        with patch('app.filedialog.askdirectory',return_value=str(self.directory)):
            e.mcp_settings();self.root.update()
        dialog=e.modals.stack[-1]
        self.assertEqual(dialog.title(),'连接外部 AI / 通用 MCP')
        self.root.tk.call(dialog.protocol('WM_DELETE_WINDOW'));self.root.update()
        self.assertFalse(dialog.winfo_exists());self.assertFalse(e.modals.stack)
        self.assertFalse(errors)


if __name__=='__main__':unittest.main()
