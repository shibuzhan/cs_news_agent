# Hacker News 讨论的配图风格

供 `app/services/image_brief.py` 按来源读取：`## 风格池` 是彼此可替换的完整视觉方案（介质、光线、
材质、镜头、色调），文章级各取一条；`## 实物池` 是候选主体，每张图各取一个。两处都只返回**编号**：
风格由规划模型按文章内容挑，实物按每张图挑，越界或模型不可用时按草稿 ID 哈希轮换。因此同一来源的
不同文章在风格与实物上都会变化，而画面始终落在下面这些已审核过的组合里。

**路线：机房、走线与工程现场的纪实与器材。** 讨论类内容适合"现场感"，全部不含人物，
避免评论截图式的伪界面。

## 风格池

1. Documentary photograph in a dim equipment room, cool ambient light with tiny status LEDs as the only highlights, deep shadows, 35mm, f/2.8, fine grain. Palette: cool grey, deep blue-grey, one warm indicator accent.
2. Available-light photograph at a night workbench, a single warm desk lamp as the key light, hard falloff into darkness, 35mm, f/2, visible grain. Palette: charcoal, amber, steel.
3. Overhead documentary photograph on a concrete floor, even cold fluorescent light, everything squared to the frame, 35mm, f/5.6, subtle grain. Palette: concrete grey, off-white, one safety yellow accent.
4. Wide environmental photograph of a server aisle in perspective, cool overhead lighting with long reflections on the floor, muted colours, 24mm, f/4, fine grain. Palette: blue-grey, black, one green indicator accent.
5. Close photograph of industrial cabling, hard raking light emphasising bundle texture, shallow depth of field, 85mm, f/2.8, fine grain. Palette: dark grey, orange cable, white.
6. Photograph in a workshop under a single overhead lamp, warm light pooling on the bench with cool shadows at the edges, 50mm, f/2.8, gentle grain. Palette: warm brown, gunmetal, one cyan accent.
7. Harsh midday photograph on a rooftop, flat overcast sky, high contrast metal surfaces, 35mm, f/5.6, fine grain. Palette: pale grey sky, galvanised steel, one rust accent.
8. Photograph of a cold metal panel, hard side light from a doorway, crisp micro-shadows and honest wear, 50mm, f/4, subtle grain. Palette: galvanised grey, off-white, one deep red accent.
9. Overhead photograph on an anti-static mat, even diffused light, small components laid out in a tidy grid, 35mm, f/5.6, very fine grain. Palette: matte black, silver, one acid green accent.
10. Photograph in a hallway of cabinets, mixed colour temperature from a warm lamp and a cool panel, 35mm, f/2.8, gentle grain. Palette: teal grey, amber, charcoal.
11. Studio photograph on a deep slate backdrop, one softbox from above, controlled falloff, 85mm, f/5.6, very fine grain. Palette: slate, off-white, one copper accent.
12. Low-angle photograph of equipment against a blank wall, single hard light from the left creating a long shadow, 24mm, f/4, fine grain. Palette: off-white wall, dark equipment, one blue accent.

## 实物池

每条以 `[类别]` 开头：同一篇文章的多张插图会避开同一类别，因此每张图的主体在类别上也是分开的。

1. [rack] a small server rack in a dim room with neat bundled cable runs and tiny status LEDs
2. [patch] the back of a rack with ethernet cables entering a patch panel
3. [night] a night workbench with a closed laptop, a ceramic mug and a paper notebook under one warm lamp
4. [whiteboard] a meeting-room whiteboard covered in erased smudges under cool window light
5. [antenna] a rooftop antenna mast and its cable entry point against an overcast sky
6. [cabling] a wall of network cables in tidy horizontal runs on a concrete wall
7. [bench] a desk corner with a switched power strip, coiled cables and a small label printer
8. [corridor] a corridor of server cabinets seen in perspective
9. [bench] a workbench with a screwdriver, a fan unit and antistatic wrapping
10. [power] a small uninterruptible power supply and coiled power cables on a concrete floor
11. [office] an empty dim office at night with one switched-off monitor and a chair pushed in
12. [fibre] a close-up of a fibre patch cable loop on a cold metal surface
13. [instrument] an oscilloscope with a plain screen and two probes coiled beside it
14. [instrument] a logic analyser with a ribbon of thin test clips fanned out
15. [power] a bench power supply with two leads and a plain display switched off
16. [instrument] a thermal camera resting on a matte surface beside a printed chart-free sheet
17. [storage] a stack of hard drives with one drive lying flat on top, no labels readable
18. [legacy] a magnetic tape cartridge and a reel resting on a metal shelf
19. [legacy] a bundle of punch cards held by a rubber band on a dark surface
20. [legacy] an old mechanical terminal keyboard with thick keycaps, no branding
21. [modules] an open relay rack drawer with spare modules in foam cut-outs
22. [conduit] a wall-mounted junction box with neatly dressed conduit
23. [cabling] a coil of outdoor ethernet cable beside a weatherproof connector
24. [tools] a laboratory-grade soldering iron resting in its stand on a metal bench
