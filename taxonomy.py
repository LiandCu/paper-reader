"""A small stable vocabulary; preserve old free-form tags as searchable keywords."""
import re
GROUPS = [
    {"name":"研究方向","tags":["大语言模型","智能体","多模态","计算机视觉","图像与视频生成","语音与音频","机器人与具身智能","世界模型","科学计算","其他方向"]},
    {"name":"方法与任务","tags":["强化学习","检索增强生成","训练与微调","蒸馏与压缩","推理与规划","记忆与长上下文","评测与基准","效率与部署","数据与合成","安全与可靠性"]},
]
CATALOG = [tag for group in GROUPS for tag in group["tags"]]
ALIASES = {
"大语言模型":["大语言模型","语言模型","llm","language model"],
"智能体":["智能体","多智能体","agent","人机协作","工具使用","工作流","harness"],
"多模态":["多模态","视觉语言","视觉-语言","vlm","multimodal","multi-modal","vision-language","音视频理解"],
"计算机视觉":["计算机视觉","视觉定位","图像分类","图像分割","目标检测","视觉理解","图像解释","image classification","image segmentation","object detection","image explanation"],
"图像与视频生成":["图像生成","视频生成","扩散模型","diffusion","image generation","video generation","text-to-image","text-to-video","交互式世界生成"],
"语音与音频":["语音","音频","声学","speech","audio","spoken"],
"机器人与具身智能":["机器人","具身","空间智能","空间推理","空间感知","空间视觉","动作空间","操作控制","robot","embodied","manipulation","locomotion"],
"世界模型":["世界模型","世界建模","物理动力学","world model","world generation","world-grounded"],
"科学计算":["科学计算","科学发现","数学证明","运筹","scientific","operations research","theorem"],
"强化学习":["强化学习","奖励模型","策略优化","强化","reinforcement learning","reward model","policy optimization"],
"检索增强生成":["检索增强","rag","retrieval-augmented","retrieval augmented","重排序","rerank"],
"训练与微调":["训练与微调","监督微调","自监督","自蒸馏","自适应","自我改进","对比学习","训练","微调","finetun","fine-tun","pretrain","pre-train","self-improvement","contrastive learning"],
"蒸馏与压缩":["蒸馏","量化","剪枝","压缩","轻量化","distill","quantization","pruning","compression"],
"推理与规划":["推理","规划","思维链","决策","reasoning","planning","chain-of-thought"],
"记忆与长上下文":["记忆","外部记忆","长上下文","长对话","memory","long context","long-context"],
"评测与基准":["评测","评估","基准","测试","benchmark","evaluation","评估方法"],
"效率与部署":["效率","计算效率","缓存","缓存在用","缓存复用","推理加速","部署","efficient","efficiency","latency","cache","deployment","speedup"],
"数据与合成":["数据","合成","监督","data","synthetic","synthesis","demonstration"],
"安全与可靠性":["安全","可靠性","可验证","鲁棒","失败归因","诊断","safety","security","robust","reliability"],
}
def _flat(text):
    return re.sub(r"[\s_\-]+","",str(text).casefold())
def _matches(alias,text):
    if alias.isascii() and alias.isalpha() and len(alias)<=4:
        return bool(re.search(r"(?<![a-z0-9])"+re.escape(alias.casefold())+r"s?(?![a-z0-9])",str(text).casefold()))
    return _flat(alias) in _flat(text)
def _scores(tags,text=""):
    scores={}
    for canonical,aliases in ALIASES.items():
        score=0
        for tag in tags:
            if tag==canonical:
                score+=100
            elif any(_matches(alias,tag) for alias in aliases):
                score+=10
        if text:
            score+=sum(1 for alias in aliases if _flat(alias) in _flat(text))
        scores[canonical]=score
    return scores
def normalize_tags(tags,title="",abstract=""):
    tags=[t.strip() for t in tags if isinstance(t,str) and t.strip()]
    scores=_scores(tags,title+" "+abstract)
    topics=[t for t in GROUPS[0]["tags"] if t!="其他方向" and scores.get(t,0)>0]
    topics.sort(key=lambda t:(-scores[t],CATALOG.index(t)))
    methods=[t for t in GROUPS[1]["tags"] if scores.get(t,0)>0]
    methods.sort(key=lambda t:(-scores[t],CATALOG.index(t)))
    # At most two research areas and two methods; no new labels are invented.
    return (topics[:2] or ["其他方向"])+methods[:2]
def validate_manual(tags):
    if not isinstance(tags,list) or len(tags)>4 or any(not isinstance(t,str) or t not in CATALOG for t in tags):
        raise ValueError("请从固定分类中选择最多 4 个标签")
    return list(dict.fromkeys(tags))
