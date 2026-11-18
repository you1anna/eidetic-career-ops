"""Bounded inert document extraction in a short-lived process; no URLs or document actions run."""
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

LIMIT=1_048_576
TEXT_LIMIT=20000
MIMES={'.txt':'text/plain','.md':'text/plain','.pdf':'application/pdf',
       '.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}

def document(value):
    if not isinstance(value,dict) or set(value)-{'filename','kind','content'}:
        raise ValueError('Invalid document')
    name=value.get('filename','')
    if not isinstance(name,str) or not name or len(name)>180 or any(c in name for c in ('/','\\','\r','\n','\x00')):
        raise ValueError('Use a plain document filename')
    suffix=Path(name).suffix.lower()
    if suffix not in MIMES:raise ValueError('Use PDF, DOCX, TXT or MD, up to 1 MiB each')
    encoded=value.get('content','')
    if not isinstance(encoded,str) or len(encoded)>1_400_000:raise ValueError('Document exceeds 1 MiB')
    try:raw=base64.b64decode(encoded,validate=True)
    except Exception:raise ValueError('Invalid document encoding') from None
    if not 0<len(raw)<=LIMIT:raise ValueError('Use a non-empty document up to 1 MiB')
    kind=value.get('kind','specification')
    if kind not in ('specification','correspondence','preparation'):raise ValueError('Choose the document evidence type')
    try:
        result=subprocess.run([sys.executable,str(Path(__file__).resolve()),suffix],input=raw,
                              capture_output=True,timeout=8,check=True)
        extracted=json.loads(result.stdout)['text']
    except (subprocess.SubprocessError,ValueError,KeyError):
        raise ValueError('Could not safely read this document. Paste its relevant text instead; scanned PDFs need text extraction first.') from None
    if not extracted.strip():raise ValueError('No readable text found. Paste the relevant text instead.')
    return dict(filename=name,kind=kind,mime=MIMES[suffix],sha=hashlib.sha256(raw).hexdigest(),
                content_base64=encoded,extracted_text=extracted)

def extract(raw,suffix):
    if suffix in ('.txt','.md'):return raw.decode('utf-8')[:TEXT_LIMIT]
    if suffix=='.docx':
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if sum(i.file_size for i in z.infolist())>8*LIMIT:raise ValueError('Expanded archive too large')
            xml=z.read('word/document.xml')
            if len(xml)>2*LIMIT or b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:raise ValueError('Invalid XML')
            root=ET.fromstring(xml)
            return '\n'.join(''.join(p.itertext()) for p in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'))[:TEXT_LIMIT]
    from pypdf import PdfReader
    reader=PdfReader(io.BytesIO(raw),strict=True)
    if reader.is_encrypted or len(reader.pages)>50:raise ValueError('Encrypted or oversized PDF')
    chunks=[];size=0
    for page in reader.pages:
        value=page.extract_text() or '';chunks.append(value);size+=len(value)
        if size>=TEXT_LIMIT:break
    return '\n'.join(chunks)[:TEXT_LIMIT]

if __name__=='__main__':
    import resource
    resource.setrlimit(resource.RLIMIT_CPU,(5,5))
    if sys.platform=='linux':resource.setrlimit(resource.RLIMIT_AS,(128*1024*1024,128*1024*1024))
    print(json.dumps(dict(text=extract(sys.stdin.buffer.read(LIMIT+1),sys.argv[1]))))
