import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from PIL import Image, ImageChops
import model as m


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_empty_render_pixel_exact(self):
        doc=m.Document(Image.new('RGBA',(32,24),(10,20,30,127)))
        self.assertEqual(m.render(doc).tobytes(),doc.image.tobytes())

    def test_render_preserves_original(self):
        doc=m.blank();before=doc.image.tobytes();doc.layers.append(m.Layer(text='中文\nTest'))
        result=m.render(doc)
        self.assertEqual(doc.image.tobytes(),before)
        self.assertNotEqual(result.tobytes(),before)
        self.assertEqual(result.size,doc.image.size)

    def test_transparent_export_has_no_background(self):
        doc=m.blank();doc.layers.append(m.Layer(text='透明文字'))
        output=m.render(doc,transparent=True)
        self.assertEqual(output.getpixel((0,0)),(0,0,0,0))
        self.assertIsNotNone(output.getchannel('A').getbbox())

    def test_hidden_layer_does_not_render(self):
        doc=m.blank();doc.layers.append(m.Layer(visible=False))
        self.assertEqual(m.render(doc).tobytes(),doc.image.tobytes())

    def test_zero_opacity_invisible(self):
        doc=m.blank();doc.layers.append(m.Layer(opacity=0))
        self.assertEqual(m.render(doc).tobytes(),doc.image.tobytes())

    def test_transfer_scaled_independent(self):
        a,b=m.blank(),m.Document(Image.new('RGBA',(2560,1440)))
        layer=m.Layer(size=42,spacing=7,outline=2,x=.2,y=.3,quad=[(.1,.1),(.4,.1),(.4,.3),(.1,.3)])
        a.layers.append(layer);copied=m.transfer(layer,a,b)
        self.assertEqual((copied.size,copied.spacing,copied.outline),(84,14,4))
        self.assertEqual((copied.x,copied.y),(.2,.3));self.assertNotEqual(layer.id,copied.id)
        copied.text='不同内容';copied.quad[0]=(.2,.2)
        self.assertNotEqual(copied.text,layer.text);self.assertNotEqual(copied.quad,layer.quad)

    def test_transfer_pixel_size_mode(self):
        a,b=m.blank(),m.Document(Image.new('RGBA',(400,400)))
        layer=m.Layer(size=56);copied=m.transfer(layer,a,b,False)
        self.assertEqual(copied.size,56)

    def test_transfer_aspect_ratio_uses_uniform_scale(self):
        a,b=m.blank(),m.Document(Image.new('RGBA',(640,720)))
        layer=m.Layer(size=40);copied=m.transfer(layer,a,b)
        self.assertEqual(copied.size,20)

    def test_project_roundtrip_embeds_images_and_layers(self):
        docs=[m.blank(),m.blank()];docs[0].layers=[m.Layer(text='可编辑\n中文',angle=10,outline=2)]
        docs[1].layers=[m.Layer(text='B 图',quad=[(.1,.2),(.8,.1),(.7,.5),(.2,.6)])]
        path=self.directory/'中文.textshift';m.save_project(path,docs)
        loaded,warnings=m.load_project(path);self.assertFalse(warnings)
        for a,b in zip(docs,loaded):
            self.assertEqual(a.image.tobytes(),b.image.tobytes())
            self.assertEqual(m.render(a).tobytes(),m.render(b).tobytes())
            self.assertEqual([x.text for x in a.layers],[x.text for x in b.layers])

    def mutate_project(self,edit):
        path=self.directory/'test.textshift';m.save_project(path,[m.blank(),m.blank()])
        with zipfile.ZipFile(path) as z:data={n:z.read(n) for n in z.namelist()}
        manifest=json.loads(data['project.json']);edit(manifest)
        data['project.json']=json.dumps(manifest).encode()
        with zipfile.ZipFile(path,'w') as z:
            for n,b in data.items():z.writestr(n,b)
        return path

    def test_project_rejects_wrong_version(self):
        path=self.mutate_project(lambda x:x.update(version=999))
        with self.assertRaises(ValueError):m.load_project(path)

    def test_project_rejects_path_traversal(self):
        path=self.mutate_project(lambda x:x['documents'][0].update(image='../secret.png'))
        with self.assertRaises(ValueError):m.load_project(path)

    def test_missing_font_warns(self):
        def edit(value):
            layer=m.asdict(m.Layer());layer['font']='Z:/missing-font.otf';value['documents'][0]['layers']=[layer]
        path=self.mutate_project(edit);docs,warnings=m.load_project(path)
        self.assertEqual(warnings,['missing-font.otf']);self.assertEqual(docs[0].layers[0].font,m.default_font())

    def test_undo_redo_and_new_action(self):
        docs=[m.blank(),m.blank()];history=m.History();history.push(docs)
        docs[0].layers.append(m.Layer());history.move(docs)
        self.assertEqual(len(docs[0].layers),0);history.move(docs,True);self.assertEqual(len(docs[0].layers),1)
        history.move(docs);history.push(docs);self.assertFalse(history.move(docs,True))

    def test_quad_identity_mapping(self):
        coeff=m.coefficients([(10,20),(110,20),(110,70),(10,70)],[(0,0),(100,0),(100,50),(0,50)])
        for (x,y),(u,v) in zip([(10,20),(110,20),(110,70),(10,70)],[(0,0),(100,0),(100,50),(0,50)]):
            den=coeff[6]*x+coeff[7]*y+1
            self.assertAlmostEqual((coeff[0]*x+coeff[1]*y+coeff[2])/den,u)
            self.assertAlmostEqual((coeff[3]*x+coeff[4]*y+coeff[5])/den,v)

    def test_crossed_quad_rejected(self):
        layer=m.Layer(quad=[(0,0),(1,1),(1,0),(0,1)])
        with self.assertRaises(ValueError):layer.validate()

    def test_perspective_renders_only_inside_quad(self):
        doc=m.blank();doc.layers=[m.Layer(quad=[(.1,.2),(.8,.15),(.75,.5),(.2,.55)])]
        out=m.render(doc,transparent=True);bbox=out.getchannel('A').getbbox()
        self.assertIsNotNone(bbox);self.assertGreaterEqual(bbox[0],128);self.assertLessEqual(bbox[2],1024)

    def test_preview_dimensions(self):
        doc=m.blank();doc.layers=[m.Layer(angle=25)]
        self.assertEqual(m.render(doc,640).size,(640,360))

    def test_invalid_limits_rejected(self):
        for kwargs in ({'size':900},{'opacity':-1},{'x':float('nan')},{'text':'a'*4001},{'color':'red'}):
            with self.subTest(kwargs=list(kwargs)),self.assertRaises(ValueError):m.Layer(**kwargs).validate()

    def test_atomic_save_failure_keeps_existing(self):
        path=self.directory/'original';path.write_bytes(b'unchanged')
        def fail(temporary):raise RuntimeError('failure')
        with self.assertRaises(RuntimeError):m.atomic_write(path,fail)
        self.assertEqual(path.read_bytes(),b'unchanged');self.assertEqual(len(list(self.directory.iterdir())),1)


if __name__=='__main__':unittest.main(verbosity=2)
