from app.gpt.prompt import BASE_PROMPT
from app.services import runtime_limits

def resolve_base_prompt() -> str:
    """本次生成实际要用的 BASE_PROMPT 模板（spec 005 ticket 11）。

    五个消费者（prompt_builder.generate_base_prompt 以及 deepseek / openai / qwen
    三个适配器里各自的 format 调用）都必须走这里，否则用户改了设置页里的覆盖层，
    走旧适配器的生成依然用内置模板 —— 「保存了但不生效」比没有这个功能更糟。

    解析顺序：数据库里的覆盖层 > 内置 BASE_PROMPT。空串（用户点了「恢复默认」，
    或从未设置过）一律回落到内置模板；读取经由 runtime_limits 的 5 秒缓存，
    不额外打库。
    """
    override = runtime_limits.base_prompt_override()
    return override or BASE_PROMPT


note_formats = [
    {'label': '目录', 'value': 'toc'},
    {'label': '原片跳转', 'value': 'link'},
    {'label': '原片截图', 'value': 'screenshot'},
    {'label': 'AI总结', 'value': 'summary'}
]

# 场景（scene）与详略（detail）是两个正交维度：场景定内容与措辞，详略定覆盖范围。
# 取值见 spec 015。前端 web 与浏览器扩展各自维护一份同名清单，一致性由
# backend/tests/015-note-preset-scene-and-detail/test_preset_vocabulary_sync.py 守住。
#
# 每段都以 **粗体标签** 起头而不是 Markdown 标题：与下面的 format 片段写法一致，
# 避免模型把新增的标题当成要复现的正文结构。
#
# 所有「按勾选追加」的片段（本文件的 format 函数，以及 prompt.py 里的 LINK / SCREENSHOT）
# 都不写行首编号：它们一次只追加一条，编号既会让模型看到孤立的「7.」，也无法保证连续
# （没勾目录时就会从 8. 直接跳到前后都没有）。编号只留给 BASE_PROMPT 里那六条恒定原则。
SCENE_PROMPTS = {
    "life": (
        "**场景：生活**\n"
        "读者是看视频的本人，几个月后回来翻看这份笔记回忆当时。\n"
        "- 用讲课人的第一人称写，像自己在复盘：当时怎么想、为什么这么做、哪里走了弯路。\n"
        "- 保留具体的人名、地点、时间、价格、数字：它们比结论更值得留。\n"
        "- 保留当时的感受和判断，但把口语整理成能读的句子。\n"
        "- 课内一带而过的经验（比如某个工具为什么好用），可以用 `> 补充：` 补一句解释，补在对应段落之后。\n"
        "- 一段话讲一件事，段与段之间空行分开。"
    ),
    "academic": (
        "**场景：学术**\n"
        "读者要引用或复述这份内容，可能在写论文、做汇报、备考——他需要能直接拿去用的严谨材料。\n"
        "- 用讲课人的第一人称讲，但严谨度按教材来：定义、条件、适用范围一个都不能含糊。\n"
        "- 术语首次出现时同时给出原文（英文 / 拉丁文），此后统一用同一个译名。\n"
        "- 一个论点一小节：先写结论句，再写支撑它的证据或推导。\n"
        "- **知识点成条**：每条写清「定义是什么」「成立条件是什么」「适用范围到哪里」，三者分开写，不要揉成一段。\n"
        "- **公式推导逐步写**：每一步写出依据（用了哪条定理、性质或等价变形），不要跳步；符号首次出现时说明含义。\n"
        "- **练习题完整保留**：题干、已知条件、选项、答案、解题步骤、最终结果一个都不能少；板书或课件上的题目同样要抄全，不能只写「讲了一道例题」。\n"
        "- **通俗讲解与严谨表述并存**：抽象概念先给一句人话解释，再给严谨定义；两者都要，不许只留一个。\n"
        "- **图内内容优先保留**：板书、课件、图表、批注上的定义、公式、表格、例题、坐标轴标注，按图上内容写进对应段落；表格转成 Markdown 表格，公式用 LaTeX。\n"
        "- 视频里没有讲清、或与常识冲突的地方，标注「视频未说明」，照实留白；确实需要补的地方用 `> 补充：` 标出。\n"
        "- 引用具体说法时附上时间点，便于回查。"
    ),
    "professional": (
        "**场景：专业**\n"
        "读者要照着这份笔记把内容用起来：做项目、跑流程、上手工具。\n"
        "- 用讲课人的第一人称写，像资深同事带你过一遍流程：讲清楚为什么这么做、哪里容易踩坑。\n"
        "- 把可执行的部分拆成有序步骤，每步写清「做什么」和「怎么判断这一步成了」。\n"
        "- 命令、参数、配置项原样抄进代码块，不改写、不省略。\n"
        "- 视频一带而过、实际操作会卡住的地方，单独写成「待确认」；能讲清的部分用 `> 补充：` 标出。\n"
        "- 界面操作、报错画面里出现的字段名、按钮名、错误原文，按画面照抄。\n"
        "- Docker、token、embedding 这类术语保留原词。"
    ),
}

DETAIL_PROMPTS = {
    "brief": (
        "**篇幅：精简**\n"
        "只写结论层：每个小节一到三句话，给出该节的结论与关键步骤。\n"
        "题目、公式、关键数字不因精简而省略；补充最多一条，只补理解所必需的前置。"
    ),
    "detailed": (
        "**篇幅：详细**\n"
        "逐段覆盖字幕内容：每个小节把该段的例子、数字、推导、原话都写进去。\n"
        "小节按视频的段落顺序排列；课内没展开但理解必需的地方，用 `> 补充：` 补上。\n"
        "同一知识点在后文被补充或修正时，在原文处补一句「后文补充」，不要静默改掉前面的说法。"
    ),
}


# 生成 BASE_PROMPT 函数
def generate_base_prompt(
    title,
    segment_text,
    tags,
    _format=None,
    scene=None,
    detail=None,
    extras=None,
    ocr_assist=False,
    approved_shots=None,
    ocr_reference=None,
):
    # 生成 Base Prompt 开头部分。模板走 resolve_base_prompt()：用户在设置页写的
    # 覆盖层在这里生效，空串则回落到内置 BASE_PROMPT（spec 005 ticket 11）。
    prompt = resolve_base_prompt().format(
        video_title=title,
        segment_text=segment_text,
        tags=tags
    )

    # OCR 参考内容：放在字幕之后、格式与风格说明之前。
    #
    # 为什么是「参考」而不是「正文」（m04445 + q2_inject_shape=A）：图上抄回来的
    # 文字只用来帮模型把内容写准，笔记的行文仍由模型自己组织。所以这里明确告诉
    # 它「不要原样照搬」，否则模型很容易把整块 OCR 结果贴成正文。
    if ocr_reference and str(ocr_reference).strip():
        prompt += (
            "\n\n## 画面文字参考（OCR）\n"
            "以下是本次判定为「值得编入笔记」的画面里识别出的全部内容，"
            "按时间点分节。它**只是参考资料**：请用它把对应段落写准确"
            "（尤其是公式、表格、板书这类抄不下来的内容），"
            "但不要原样照搬整块内容，也不要为它单独开一节。\n"
            "每个 `[[SHOT-n]]` 仍然只对应一张即将插入的截图。\n\n"
            + str(ocr_reference).strip()
        )

    # 添加用户选择的格式
    if _format:
        prompt += "\n" + "\n".join([
            get_format_function(f, ocr_assist=ocr_assist, approved_shots=approved_shots)
            for f in _format
        ])

    # 根据用户选择的笔记场景与详略添加描述。
    # 顺序固定为「场景 → 详略」：场景先定读者与措辞，详略再定覆盖范围。
    if scene:
        prompt += "\n" + get_scene_prompt(scene)
    if detail:
        prompt += "\n" + get_detail_prompt(detail)

    # 添加额外内容
    if extras:
        prompt += f"\n{extras}"
    return prompt


# 获取格式函数
def get_format_function(format_type, ocr_assist=False, approved_shots=None):
    # 已判定画面使用编号占位符，独立于额外的 OCR 文字识别开关。
    # None 保留旧调用方契约；空清单表示本次没有可插入的截图。
    if format_type == 'screenshot' and (ocr_assist or approved_shots is not None):
        return get_ocr_assist_screenshot_format(approved_shots or [])
    format_map = {
        'toc': get_toc_format,
        'link': get_link_format,
        'screenshot': get_screenshot_format,
        'summary': get_summary_format
    }
    return format_map.get(format_type, lambda: '')()


# 场景描述的处理
def get_scene_prompt(scene):
    """场景片段：读者是谁、他拿这份笔记做什么、内容与措辞怎么定。"""
    if not scene:
        return ''
    return SCENE_PROMPTS.get(scene, '')


# 详略描述的处理
def get_detail_prompt(detail):
    """详略片段：只定覆盖范围，不动内容与措辞。"""
    if not detail:
        return ''
    return DETAIL_PROMPTS.get(detail, '')


def get_toc_format():
    return """
    **目录**: 在正文最前面写一个小节，二级标题固定为 `## 目录`，紧跟一个无序列表：
    每个 `##` 级章节一行，格式为 `- 章节标题`（标题文字与后文章节标题逐字一致，不带时间标记、不带链接）。
    目录里只列标题，不要写正文、也不要把后面章节的标题原样再抄一遍。
    目录之后另起一个 `##` 小节开始写正文。
    """

def get_link_format():
    return '''
    **原片跳转**: 为每个主要章节添加时间戳，使用格式 `*Content-[mm:ss]`。 
    重要：**始终**在章节标题前加上 `*Content` 前缀，例如：`AI 的发展史 *Content-[01:23]`。一定是标题在前 插入标记在后
    '''


def get_screenshot_format():
    return '''
**原片截图**:你收到的截图一般是一个网格，网格的每张图片就是一个时间点，左上角会包含时间mm:ss的格式，请你结合我发你的图片插入截图提示，请你帮助用户更好的理解视频内容，请你认真的分析每个图片和对应的转写文案，插入最合适的内容来备注用户理解，请一定按照这个格式 返回否则系统无法解析：
- 格式：`*Screenshot-[mm:ss]`

    '''


def get_ocr_assist_screenshot_format(approved_shots):
    """OCR 辅助模式下的截图指令：只允许使用判定通过的编号占位符。

    为什么不再用 `*Screenshot-[mm:ss]`（spec 008 §5.6e）：
      - 那个正则的分钟位是两位数字，超过 99 分钟的时间点根本表达不出来；
      - 编号被结构性约束在判定清单内，模型无法凭空插入一个没被判定的时间点。
    """
    from app.services.ocr_assist import render_shot_table

    if not approved_shots:
        return '''
**原片截图**: 本次没有任何画面通过价值判定，请不要插入任何截图标记。
'''

    table = render_shot_table(approved_shots)
    return f'''
**原片截图**: 本次已由画面价值判定筛出下列 {len(approved_shots)} 个截图点。
你必须在正文最合适的位置为**每一个**编号插入一次标记，格式严格为 `[[SHOT-n]]`：

{table}

要求：
- 只能使用上表列出的编号，禁止自己发明编号或时间点；
- 每个编号必须出现且只出现一次，并且紧跟在为它写的说明文字之后；
- 除 `[[SHOT-n]]` 外，不要再写 `*Screenshot-...` 之类的其它截图标记。
'''


def get_summary_format():
    return '''
    **AI总结**: 在笔记末尾加一段简短的总结，二级标题固定为 `## AI 总结`。
    总结用讲课人的口吻回顾整节课的脉络（讲了什么、结论是什么、最该记住的是哪几点），
    不要引入正文里没有的新内容。
    '''
