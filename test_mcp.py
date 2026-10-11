import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from mcp_server import Server,serve,definitions


class McpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.server=Server(self.root)
        self.init=self.request('initialize',{'protocolVersion':'2025-11-25'})
        self.server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})

    def request(self,method,params):
        return self.server.handle({'jsonrpc':'2.0','id':1,'method':method,'params':params})

    def call(self,name,**args):
        result=self.request('tools/call',{'name':'textshift_'+name,'arguments':args})['result']
        self.assertFalse(result.get('isError'),result)
        return json.loads(result['content'][0]['text'])

    def failure(self,name,**args):
        result=self.request('tools/call',{'name':'textshift_'+name,'arguments':args})['result']
        self.assertTrue(result.get('isError'),result)

    def test_handshake_schema_and_preview(self):
        self.assertEqual(self.init['result']['protocolVersion'],'2025-11-25')
        from jsonschema import Draft202012Validator
        for t in definitions():Draft202012Validator.check_schema(t['inputSchema'])
        self.assertEqual(len(self.request('tools/list',{})['result']['tools']),21)
        result=self.request('tools/call',{'name':'textshift_preview','arguments':{'width':64}})['result']
        self.assertEqual(result['content'][0]['type'],'image')

    def test_full_edit_transfer_export_roundtrip(self):
        image=self.root/'source.png';Image.new('RGBA',(500,200),'white').save(image)
        original=image.read_bytes();self.call('open_image',path='source.png')
        layer=self.call('add_layer',properties={'text':'A neutral test','size':28,'x':.2})
        self.call('move_layer',layer_id=layer['id'],dx=1,dy=2)
        self.call('center_layer',layer_id=layer['id'],axis='x')
        self.call('transfer');self.call('export',canvas='B',path='result.png')
        self.call('save_project',path='result.textshift')
        self.call('open_project',path='result.textshift',replace=True)
        self.assertEqual(len(self.call('state')['documents'][1]['layers']),1)
        self.assertEqual(image.read_bytes(),original)

    def test_records_mask_repair_generation_and_undo(self):
        rows=[{'id':'r','source':'TEST','target':'测试','score':.9,'box':[[.1,.1],[.8,.1],[.8,.4],[.1,.4]]}]
        self.call('set_records',records=rows)
        self.call('make_mask',mode='box')
        self.call('repair_background',method='solid',color='#ffffff')
        layers=self.call('make_layers')['layers'];self.assertEqual(layers[0]['text'],'测试')
        self.call('undo');self.assertFalse(self.call('state')['documents'][0]['layers'])

    def test_workspace_traversal_and_overwrite_protection(self):
        self.failure('export',path='../escape.png')
        image=self.root/'source.png';Image.new('RGB',(100,100)).save(image)
        self.call('open_image',path='source.png')
        self.failure('export',path='source.png',overwrite=True)
        self.call('export',path='out.png');self.failure('export',path='out.png')
        self.call('export',path='out.png',overwrite=True)

    def test_schema_rejections_do_not_mutate(self):
        before=self.call('state')
        self.failure('add_layer',properties={'size':True})
        self.failure('add_layer',properties={'text':'test','opacity':101})
        self.failure('add_layer',properties={'id':'fake'})
        self.failure('add_layer',properties={'x':float('nan')})
        self.failure('add_layer',properties={'font':'C:/Windows/win.ini'})
        self.failure('open_image',canvas='C',path='anything.png')
        self.assertEqual(self.call('state'),before)

    def test_ocr_not_auto_styled_and_original_only(self):
        with patch('automation.imaging.recognize',return_value=[]) as recognize:
            self.call('ocr')
            self.assertIs(recognize.call_args.args[0],self.server.session.docs[0].original)

    def test_invalid_protocol_and_notification(self):
        out=io.StringIO()
        serve(self.server,io.StringIO('bad json\n{"jsonrpc":"2.0","method":"notifications/cancelled"}\n'),out)
        lines=out.getvalue().splitlines();self.assertEqual(len(lines),1)
        self.assertEqual(json.loads(lines[0])['error']['code'],-32700)
        self.assertEqual(self.server.handle([])['error']['code'],-32600)
        unready=Server(self.root)
        self.assertIn('error',unready.handle({'jsonrpc':'2.0','id':1,'method':'tools/list'}))

    def test_real_stdio_subprocess(self):
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}},
                  {'jsonrpc':'2.0','method':'notifications/initialized'},
                  {'jsonrpc':'2.0','id':2,'method':'tools/list'},
                  {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'textshift_state','arguments':{}}}]
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('mcp_server.py')),'--workspace',str(self.root)],
                              input=''.join(json.dumps(m)+'\n' for m in messages),text=True,encoding='utf-8',capture_output=True,timeout=40)
        self.assertEqual(result.returncode,0,result.stderr)
        replies=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([x['id'] for x in replies],[1,2,3]);self.assertFalse(replies[-1]['result']['isError'])


if __name__=='__main__':unittest.main()
