# Shared data/binaries; distinct GUI and console entrypoints (stdio needs console build).
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

root=Path(SPECPATH)
datas=collect_data_files('rapidocr')+collect_data_files('onnxruntime')+collect_data_files('jsonschema_specifications')
binaries=collect_dynamic_libs('onnxruntime')
exclude=['matplotlib','pytest','IPython','torch','torchvision','paddle','openvino','tensorrt','MNN','scipy','pandas']
common=dict(pathex=[str(root)],binaries=binaries,datas=datas,hookspath=[str(root/'hooks')],
            hiddenimports=['rapidocr.inference_engine.onnxruntime'],excludes=exclude)
gui=Analysis([str(root/'app.py')],**common)
mcp=Analysis([str(root/'mcp_server.py')],**common)
gui_exe=EXE(PYZ(gui.pure),gui.scripts,[],exclude_binaries=True,name='TextShift',console=False,upx=False)
mcp_exe=EXE(PYZ(mcp.pure),mcp.scripts,[],exclude_binaries=True,name='TextShiftMCP',console=True,upx=False)
bundle=COLLECT(gui_exe,mcp_exe,gui.binaries,gui.datas,mcp.binaries,mcp.datas,name='TextShift',upx=False)
