"""Standalone GUI + MCP executables, shared runtime, no private workspace paths.

Install requirements and PyInstaller first. OCR models must already be present in
rapidocr/models (no model downloading here). Output can live on another disk.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
import zipfile

ROOT=Path(__file__).parent.resolve()
SOURCE_FILES=['__init__.py','app.py','model.py','imaging.py','style_match.py','layout.py',
              'workflow.py','window_manager.py','ai_client.py','automation.py','mcp_server.py',
              'smoke_test.py','test_model.py','test_ui.py','test_workflow.py','test_windows.py',
              'test_layout.py','test_mcp.py','requirements.txt','pyproject.toml','README.txt',
              'build_release.py','TextShift.spec','.gitignore']


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();output=args.output.resolve();dist=output/'dist'
    if output.exists() and any(output.iterdir()):raise ValueError('Build output must be new or empty.')
    import rapidocr
    models=Path(rapidocr.__file__).parent/'models'
    required=('PP-OCRv6_det_small.onnx','PP-OCRv6_rec_small.onnx','ch_ppocr_mobile_v2.0_cls_mobile.onnx')
    for name in required:
        if not (models/name).is_file():raise ValueError('Missing offline model: '+name)
    subprocess.run([sys.executable,'-m','PyInstaller','--distpath',str(dist),
                    '--workpath',str(output/'build'),str(ROOT/'TextShift.spec')],check=True)
    package=dist/'TextShift';source=package/'source';source.mkdir()
    for name in SOURCE_FILES:shutil.copy2(ROOT/name,source/name)
    shutil.copytree(ROOT/'hooks',source/'hooks',ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(ROOT/'README.txt',package/'先读我.txt')
    licenses=package/'licenses';licenses.mkdir()
    selected={'pillow','rapidocr','onnxruntime','opencv-python-headless','numpy','fonttools','requests',
              'jsonschema','jsonschema-specifications','referencing','rpds-py','attrs','charset-normalizer',
              'certifi','idna','urllib3','pyyaml','omegaconf','antlr4-python3-runtime','colorlog','colorama',
              'tqdm','packaging','flatbuffers','protobuf','pyclipper','shapely','six','sympy','mpmath',
              'cryptography','cffi','pycparser','setuptools','coloredlogs','humanfriendly','typing-extensions'}
    copied=set()
    for distribution in importlib.metadata.distributions():
        name=distribution.metadata.get('Name','').lower().replace('_','-')
        if name not in selected or name in copied:continue
        copied.add(name)
        for item in distribution.files or []:
            file=distribution.locate_file(item)
            if file.is_file() and (file.name=='METADATA' or any(s in file.name.upper() for s in ('LICENSE','COPYING','NOTICE'))):
                relative=Path(*[part for part in item.parts if part not in ('..','.')])
                target=licenses/name/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,target)
    for file in [Path(sys.base_prefix)/'LICENSE.txt',Path(sys.base_prefix)/'tcl/tcl8.6/license.terms',Path(sys.base_prefix)/'tcl/tk8.6/license.terms']:
        if file.is_file():shutil.copy2(file,licenses/(file.parent.name+'-'+file.name))
    (licenses/'OCR-MODELS-SHA256.json').write_text(json.dumps({n:sha(models/n) for n in required},indent=2),encoding='utf-8')
    files={str(f.relative_to(package)).replace('\\','/'):sha(f) for f in sorted(package.rglob('*')) if f.is_file()}
    (package/'FILES_SHA256.json').write_text(json.dumps(files,ensure_ascii=False,indent=2),encoding='utf-8')
    archive=dist/'TextShift-0.3.0-Windows.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for f in package.rglob('*'):
            if f.is_file():z.write(f,'TextShift/'+f.relative_to(package).as_posix())
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for name,digest in files.items():assert hashlib.sha256(z.read('TextShift/'+name)).hexdigest()==digest
    report={'version':'0.3.0','archive':str(archive),'sha256':sha(archive),'bytes':archive.stat().st_size,'files':len(files)+1}
    (output/'build-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':main()
