# arXiv 论文的配图风格

供 `app/services/image_brief.py` 按来源读取：`## 风格池` 是彼此可替换的完整视觉方案（介质、光线、
材质、镜头、色调），文章级各取一条；`## 实物池` 是候选主体，每张图各取一个。两处都只返回**编号**：
风格由规划模型按文章内容挑，实物按每张图挑，越界或模型不可用时按草稿 ID 哈希轮换。因此同一来源的
不同文章在风格与实物上都会变化，而画面始终落在下面这些已审核过的组合里。

**路线：研究场景的静物与器材。** 论文内容偏抽象，用真实的纸张、量具与实验器材建立可信质感，
避免"概念图 / 信息图"路线——那类构图最容易生成伪文字，会被本地 OCR 质量门拒掉。

## 风格池

1. Editorial still-life photograph on a seamless neutral paper backdrop, hard directional daylight from the upper left casting one crisp shadow, deep negative space, matte materials, 50mm, f/4, fine film grain. Palette: off-white, graphite grey, one muted amber accent.
2. Macro photograph of a paper surface, raking hard light revealing fibre texture, very shallow depth of field, 100mm macro, f/4, fine grain. Palette: paper white, soft grey, one faint blue.
3. Top-down flat-lay photograph on blank graph paper, even diffused daylight with no harsh shadows, everything squared to the frame, 35mm, f/5.6, gentle grain. Palette: off-white, pale blue grid, graphite.
4. Photograph on a dark laboratory bench, a single cool task lamp as the key light, deep shadows and one bright highlight, 50mm, f/2.8, visible grain. Palette: charcoal, cool white, one cyan accent.
5. Wide environmental photograph of a quiet study corner, soft daylight through tall windows, muted colours, generous empty wall space, 24mm, f/4, fine grain. Palette: warm grey, pale wood, one dusty green.
6. Still-life photograph on a slate surface, hard side light from a single window, crisp micro-shadows, 50mm, f/5.6, subtle grain. Palette: slate grey, chalk white, one burnt orange accent.
7. Photograph on a linen cloth backdrop, soft directional light with gentle falloff, tactile fabric texture, 85mm, f/2.8, fine grain. Palette: oat, warm grey, one ink blue accent.
8. Close photograph of glassware on a matte tray, cool diffused light from above, clean edges and soft reflections, 85mm, f/5.6, very fine grain. Palette: pale blue-grey, white, one amber accent.
9. Overhead photograph on a worn wooden bench, hard afternoon light with long shadows, visible dust and micro-scratches, 50mm, f/4, film grain. Palette: walnut brown, cream, one deep teal accent.
10. Studio photograph on a deep navy seamless backdrop, one softbox from the side, controlled falloff into shadow, 85mm, f/5.6, very fine grain. Palette: deep navy, off-white, brass.
11. Photograph on a brushed steel surface, cool fluorescent light from above, precise metallic reflections, 50mm, f/4, fine grain. Palette: steel grey, white, one signal red accent.
12. Soft backlit photograph against a bright window, subjects reduced to clean silhouettes with detail held in the shadows, 35mm, f/4, gentle grain. Palette: bright white, pale grey, one warm accent.

## 实物池

每条以 `[类别]` 开头：同一篇文章的多张插图会避开同一类别，因此每张图的主体在类别上也是分开的。

1. [paper] a small stack of blank paper sheets with visible paper grain
2. [writing] a sharpened pencil resting on a folded sheet of blank paper
3. [optics] a single glass lens element on a matte grey surface
4. [measure] a thin metal ruler beside a folded sheet of blank graph paper
5. [desk] a closed notebook with a ceramic cup in a quiet desk corner
6. [chalk] erased chalk smudges and a felt eraser on a matte slate surface
7. [desk] a pair of reading glasses beside a closed hardcover notebook
8. [stationery] a chain of paper clips resting on a blank sheet of paper
9. [paper] a rolled sheet of blank paper held by a rubber band, standing upright
10. [glassware] a clear glass beaker of plain water on a matte surface under hard side light
11. [stationery] a wooden pencil cup holding three rolled blank paper scrolls
12. [paper] a stack of blank index cards with one card leaning against it
13. [microscope] a laboratory microscope with a plain stage, no branding
14. [pipette] a rack of micropipettes of different sizes on a matte bench
15. [glassware] a stack of empty Petri dishes beside a glass stirring rod
16. [tubes] a centrifuge tube rack holding empty tubes with plain caps
17. [measure] a pair of metal calipers resting on a blank sheet of paper
18. [drafting] a drafting compass and a set square on blank graph paper
19. [blueprint] a rolled technical blueprint with a rubber band, seen at an angle
20. [slides] a slide storage box with the lid ajar, plain slides visible
21. [clamp] a laboratory clamp stand with a single empty clamp
22. [balance] a digital balance with a plain weighing boat, display switched off
23. [optics] a prism splitting a beam on a matte black surface
24. [magnet] a set of small magnets and iron filings on a blank card
