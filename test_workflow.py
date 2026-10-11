import base64
import copy
import io
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock
import zipfile
from PIL import Image, ImageDraw, ImageFont
import numpy as np
import model as m
import imaging
import ai_client as ai
import style_match
from app import Editor
from workflow import Workflow


def record(id='r1',source='测试文本'):
    return {'id':id,'source':source,'target':'','score':.95,'box':[[.1,.1],[.8,.1],[.8,.35],[.1,.35]]}


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.doc=m.Document(Image.new('RGBA',(240,120),(220,210,200,127)))
        self.doc.records=[record()]

    def test_mask_does_not_touch_transparent_pixels(self):
        im=self.doc.image.copy();im.putalpha(0)
        mask=imaging.text_mask(im,self.doc.records,255,3,'box')
        self.assertIsNone(mask.getbbox())

    def test_local_repair_outside_mask_and_alpha_exact(self):
        mask=Image.new('L',self.doc.image.size);ImageDraw.Draw(mask).rectangle((30,30,90,65),fill=255)
        for method in ('telea','ns','solid'):
            output=imaging.repair(self.doc.image,mask,method)
            a=np.asarray(self.doc.image);b=np.asarray(output);outside=np.asarray(mask)==0
            self.assertTrue(np.array_equal(a[outside],b[outside]));self.assertTrue(np.array_equal(a[:,:,3],b[:,:,3]))

    def test_candidate_dimensions_rejected(self):
        with self.assertRaises(ValueError):imaging.apply_candidate(self.doc.image,Image.new('RGBA',(100,100)),self.doc.mask)

    def test_empty_mask_rejected(self):
        with self.assertRaises(ValueError):imaging.repair(self.doc.image,self.doc.mask)

    def test_clone_preserves_original_and_alpha(self):
        im=self.doc.image.copy();ImageDraw.Draw(im).rectangle((10,10,30,30),fill=(30,40,50,127));before=im.tobytes()
        out=imaging.clone_patch(im,(100,70),(20,20),8)
        self.assertEqual(out.getpixel((100,70)),(30,40,50,127));self.assertEqual(im.tobytes(),before)
        self.assertEqual(out.getchannel('A').tobytes(),im.getchannel('A').tobytes())

    def test_layer_generation_translation_and_independence(self):
        self.doc.records[0]['target']='译文';layers=imaging.layers_from_records(self.doc,self.doc.records)
        self.assertEqual(layers[0].text,'译文');self.assertEqual(self.doc.layers,[])
        layers[0].text='独立';self.assertEqual(self.doc.records[0]['target'],'译文')

    def test_image_mask_records_undo(self):
        docs=[self.doc,m.blank()];history=m.History();before=self.doc.image.tobytes();history.push(docs)
        self.doc.image=Image.new('RGBA',self.doc.image.size,'red');self.doc.mask=Image.new('L',self.doc.image.size,255);self.doc.records[0]['target']='changed'
        history.move(docs);self.assertEqual(self.doc.image.tobytes(),before);self.assertFalse(self.doc.mask.getbbox());self.assertEqual(self.doc.records[0]['target'],'')
        history.move(docs,True);self.assertEqual(self.doc.records[0]['target'],'changed')

    def test_project_v2_roundtrip_and_no_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.textshift';self.doc.mask=Image.new('L',self.doc.image.size,128)
            self.doc.image=Image.new('RGBA',self.doc.image.size,'green');m.save_project(path,[self.doc,m.blank()]);docs,_=m.load_project(path)
            self.assertEqual(docs[0].original.tobytes(),self.doc.original.tobytes());self.assertEqual(docs[0].mask.tobytes(),self.doc.mask.tobytes());self.assertEqual(docs[0].records,self.doc.records)
            with zipfile.ZipFile(path) as z:self.assertNotIn('key',z.read('project.json').decode())

    def test_project_v1_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.textshift';buf=io.BytesIO();self.doc.image.save(buf,format='PNG')
            data={'format':'textshift','version':1,'documents':[{'image':f'image-{i}.png','title':'old','layers':[]} for i in range(2)]}
            with zipfile.ZipFile(path,'w') as z:
                z.writestr('project.json',json.dumps(data))
                for i in range(2):z.writestr(f'image-{i}.png',buf.getvalue())
            docs,_=m.load_project(path);self.assertEqual(docs[0].original.tobytes(),docs[0].image.tobytes())

    def test_records_invalid_nan_duplicate_and_folded(self):
        for rows in ([record(),record()],[dict(record(),score=float('nan'))],[dict(record(),box=[[0,0],[1,1],[1,0],[0,1]])]):
            with self.assertRaises(ValueError):m.validate_records(rows)

    def test_font_inspection_missing_glyph(self):
        self.doc.layers=[m.Layer(text='中文',font='C:/Windows/Fonts/arial.ttf')]
        self.assertIn('字体缺字',imaging.inspection(self.doc))

    def test_per_line_size_font_weight_and_color(self):
        image=Image.new('RGBA',(1000,360),'#f4f0e8');pen=ImageDraw.Draw(image);rows=[]
        examples=[('植物观察日记','msyhbd.ttc',58,'#923c32',25),('今天在图书馆读书','simsun.ttc',36,'#30694d',145),('TEXT SHIFT','arial.ttf',30,'#3c497e',250)]
        for i,(text,name,size,color,y) in enumerate(examples):
            font=ImageFont.truetype('C:/Windows/Fonts/'+name,size);pen.text((40,y),text,font=font,fill=color)
            a,b,c,d=pen.textbbox((40,y),text,font=font)
            row=record('r'+str(i),text);row['box']=[[(a-5)/1000,(b-5)/360],[(c+5)/1000,(b-5)/360],[(c+5)/1000,(d+5)/360],[(a-5)/1000,(d+5)/360]];rows.append(row)
        styled=style_match.estimate_rows(image,rows)
        for row,(_,name,size,color,_) in zip(styled,examples):
            with self.subTest(font=name):
                style=row['style'];self.assertEqual(Path(style['font']).name.lower(),name)
                self.assertAlmostEqual(style['size'],size,delta=4);self.assertEqual(style['color'],color)
        doc=m.Document(image);layers=imaging.layers_from_records(doc,styled,auto_style=True,sensitivity=100)
        self.assertEqual([l.color for l in layers],[e[3] for e in examples]);self.assertIn('bd',Path(layers[0].font).stem)
        self.assertGreater(layers[0].size,layers[1].size);self.assertGreater(layers[1].size,layers[2].size)

    def test_light_text_style_on_dark_background(self):
        image=Image.new('RGBA',(400,120),'#183a31');pen=ImageDraw.Draw(image);font=ImageFont.truetype(m.default_font(),38)
        pen.text((30,20),'植物观察',font=font,fill='#f3eac4');row=record(source='植物观察');row['box']=[[.05,.1],[.5,.1],[.5,.8],[.05,.8]]
        style=style_match.estimate_line(image,row);self.assertEqual(style['color'],'#f3eac4')

    def test_style_roundtrip_validation_and_fallback(self):
        row=record();row['style']={'font':m.default_font(),'size':42,'color':'#96352f','outline':1,'match_score':.8}
        m.validate_records([row]);self.doc.records=[row]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'styled.textshift';m.save_project(path,[self.doc,m.blank()]);loaded,_=m.load_project(path)
            self.assertEqual(loaded[0].records[0]['style'],row['style'])
        row['style']['size']=6000
        with self.assertRaises(ValueError):m.validate_records([row])

    def test_real_offline_ocr_chinese_japanese_english(self):
        image=Image.new('RGBA',(1000,320),'white');pen=ImageDraw.Draw(image);font=ImageFont.truetype(m.default_font(),42)
        lines=['城市植物观察日记','今日は図書館で本を読みます。','TEXT SHIFT 2026']
        for y,text in zip((30,120,220),lines):pen.text((35,y),text,font=font,fill='black')
        with patch('socket.socket.connect',side_effect=AssertionError('OCR attempted network')):
            rows=imaging.recognize(image)
            cropped=imaging.recognize(image,(0,95,1000,200))
        self.assertEqual([r['source'] for r in rows],lines)
        # Detector may split a single physical line after cropping; UI offers merge/order.
        self.assertEqual(''.join(r['source'] for r in cropped),lines[1])
        self.assertGreater(cropped[0]['box'][0][1],.3)


class AITests(unittest.TestCase):
    def setUp(self):self.config=ai.Config(key='TEST-SECRET-NOT-REAL',text_model='test-text',image_model='test-image',enabled=True)

    def test_disabled_never_calls_network(self):
        self.config.enabled=False
        with patch('ai_client.request') as request,self.assertRaises(ValueError):ai.translate(self.config,[record()])
        request.assert_not_called();self.assertNotIn(self.config.key,repr(self.config))

    def test_rejects_insecure_url_and_embedded_credentials(self):
        for url in ['http://localhost/v1','https://key@example.com/v1','https://example.com/v1?secret=x']:
            self.config.base_url=url
            with self.assertRaises(ValueError):self.config.validate('text')

    def response(self,rows,finish='stop'):
        return {'choices':[{'finish_reason':finish,'message':{'content':json.dumps({'translations':rows})}}]}

    def test_translation_row_mapping(self):
        with patch('ai_client.request',return_value=self.response([{'id':'r1','target':'Translation'}])) as request:
            self.assertEqual(ai.translate(self.config,[record()]),{'r1':'Translation'})
            payload=request.call_args.kwargs['json'];self.assertEqual(payload['response_format'],{'type':'json_object'})

    def test_translation_incomplete_refusal_and_truncation_preserve_data(self):
        rows=[record()];before=copy.deepcopy(rows)
        for payload in [self.response([]),self.response([{'id':'r1','target':'x'}],'length'),self.response([{'id':'r1','target':'x'},{'id':'r1','target':'y'}]),{'choices':[{'finish_reason':'stop','message':{'refusal':'No'}}]}]:
            with patch('ai_client.request',return_value=payload),self.assertRaises(ValueError):ai.translate(self.config,rows)
            self.assertEqual(rows,before)

    def test_request_no_redirect_no_environment_credentials(self):
        session=MagicMock();response=MagicMock();response.status_code=200;response.iter_content.return_value=[b'{"ok":true}'];response.__enter__.return_value=response
        session.post.return_value=response;session.__enter__.return_value=session
        with patch('ai_client.requests.Session',return_value=session):self.assertEqual(ai.request(self.config,'/test',json={}),{'ok':True})
        self.assertFalse(session.trust_env);self.assertFalse(session.post.call_args.kwargs['allow_redirects'])

    def test_http_failure_does_not_echo_secret_or_retry(self):
        session=MagicMock();response=MagicMock();response.status_code=400;response.text=self.config.key;response.__enter__.return_value=response
        session.post.return_value=response;session.__enter__.return_value=session
        with patch('ai_client.requests.Session',return_value=session),self.assertRaises(ValueError) as caught:ai.request(self.config,'/test')
        self.assertNotIn(self.config.key,str(caught.exception));session.post.assert_called_once()

    def test_image_mask_transparent_edit_regions(self):
        image=Image.new('RGBA',(80,60),'white');mask=Image.new('L',image.size);mask.putpixel((20,20),255)
        buf=io.BytesIO();image.save(buf,format='PNG')
        with patch('ai_client.request',return_value={'data':[{'b64_json':base64.b64encode(buf.getvalue()).decode()}]}) as request:
            out=ai.edit_background(self.config,image,mask,'restore texture');self.assertEqual(out.size,image.size)
            sent=Image.open(io.BytesIO(request.call_args.kwargs['files']['mask'][1]));self.assertEqual(sent.getpixel((20,20))[3],0);self.assertEqual(sent.getpixel((0,0))[3],255)


class WorkflowUiTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw();self.editor=Editor(self.root);self.editor.docs[0]=m.Document(Image.new('RGBA',(800,500),'white'))
        self.editor.docs[0].records=[record()];self.flow=Workflow(self.editor,0);self.root.update();self.addCleanup(self.cleanup)

    def cleanup(self):
        self.flow.closed=True
        if self.flow.timer:self.flow.win.after_cancel(self.flow.timer)
        if self.editor.pending:self.root.after_cancel(self.editor.pending)
        self.root.destroy()

    def test_edit_generate_mask_repair_accept_undo(self):
        f=self.flow;f.table.selection_set('r1');f.select();f.target.insert('1.0','新译文');f.save_row()
        self.assertEqual(f.doc.records[0]['target'],'新译文');f.make_layers();self.assertEqual(f.doc.layers[0].text,'新译文')
        f.mask_mode.set('整框');f.generate_mask();self.assertIsNotNone(f.doc.mask.getbbox())
        out=imaging.repair(f.doc.image,f.doc.mask,'solid',color='#ff0000');f.show_candidate(out);before=f.doc.image.tobytes();f.accept()
        self.assertNotEqual(f.doc.image.tobytes(),before);f.undo();self.assertEqual(f.doc.image.tobytes(),before)

    def test_oversized_row_edit_blocks_selection_change(self):
        f=self.flow;f.doc.records.append(record('r2'));f.refresh();f.table.selection_set('r1');f.select();f.source.delete('1.0','end');f.source.insert('1.0','a'*4001)
        f.table.selection_set('r2')
        with patch('workflow.messagebox.showerror'):f.select()
        self.assertEqual(f.selected,'r1');self.assertEqual(f.table.selection(),('r1',));self.assertEqual(len(f.source.get('1.0','end-1c')),4001)

    def test_manual_roi_mask_and_zoom_geometry(self):
        f=self.flow;f.zoom.set('100%');f.draw();f.mode.set('矩形蒙版')
        f.press(SimpleNamespace(x=40,y=50));f.release(SimpleNamespace(x=90,y=100))
        self.assertEqual(f.doc.mask.getbbox(),(40,50,91,101));f.undo();self.assertIsNone(f.doc.mask.getbbox())
        f.roi=(100,100,300,160);f.add_row();self.assertEqual(len(f.doc.records),2)

    def test_cancelled_job_result_is_not_applied(self):
        f=self.flow;f.run('test',lambda:123,lambda n:f.doc.records.clear());f.cancel()
        deadline=time.monotonic()+2
        while f.results.empty() and time.monotonic()<deadline:time.sleep(.01)
        f.poll();self.assertEqual(len(f.doc.records),1)

    def test_busy_does_not_start_second_job(self):
        f=self.flow;f.busy=True
        with patch('workflow.threading.Thread') as thread:f.run('second',lambda:1,lambda n:None)
        thread.assert_not_called()

    def test_api_consent_cancel_never_sends(self):
        f=self.flow;self.editor.ai_config=ai.Config(key='not-real',enabled=True,text_model='test')
        with patch('workflow.messagebox.askyesno',return_value=False),patch('workflow.ai_client.translate') as request:f.translate()
        request.assert_not_called()

    def test_reorder_and_merge(self):
        f=self.flow;f.doc.records.append(record('r2','second'));f.refresh();f.table.selection_set('r2');f.select();f.reorder(-1)
        self.assertEqual(f.doc.records[0]['id'],'r2');f.table.selection_set('r1','r2');f.merge_rows()
        self.assertEqual(len(f.doc.records),1);self.assertEqual(f.doc.records[0]['source'],'second\n测试文本')


if __name__=='__main__':unittest.main(verbosity=2)
