#!/usr/bin/env python3
from PIL import Image, ImageDraw
from pathlib import Path
im=Image.new('RGBA',(1024,1024),(0,0,0,0));d=ImageDraw.Draw(im)
d.rounded_rectangle((20,20,1004,1004),radius=220,fill='#122A36')
d.rounded_rectangle((148,248,876,664),radius=116,fill='#D9ECE9')
for x in (344,680):
 d.ellipse((x-124,332,x+124,580),fill='#163E4B')
 d.ellipse((x-78,378,x+78,534),fill='#40B8AD')
 d.ellipse((x-31,402,x+8,441),fill='#D9FFF6')
d.line((512,630,512,848),fill='#40B8AD',width=64)
d.polygon([(400,770),(512,892),(624,770)],fill='#40B8AD')
im.save(Path(__file__).resolve().parents[1]/'resources/icon.png')
