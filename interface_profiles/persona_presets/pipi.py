"""pipi 用户画像预设模板。

抽取自 web_admin.py 的 PERSONA_TEMPLATES 常量，按 target_api 动态加载：
    from interface_profiles import load_persona_presets
    templates = load_persona_presets('pipi')

新增接口时在 persona_presets/<target_api>.py 定义同结构的 PERSONA_TEMPLATES dict。
"""

# ─── 用户类型预设模板 ─────────────────────────────────
# 目标用户群体：15-34岁女性，三线及以上城市，喜欢毛绒玩具，有宠物或喜欢宠物
# 消费特点：视觉吸引→情绪共鸣→社交谈资→瞬间下单，颜值正义，情绪消费
PERSONA_TEMPLATES = {
    # ===== 核心目标用户 =====
    "plush_lover_student": {
        "name": "毛绒控学生党",
        "description": "18-24岁女性，大学生或刚毕业，喜欢毛绒玩具和改娃，重度小红书/B站用户",
        "profile": {
            "age": lambda: str(random.randint(18, 24)),
            "gender": lambda: "女",
            "city": lambda: random.choice(["杭州", "成都", "南京", "武汉", "西安", "长沙", "郑州", "合肥"]),
            "occupation": lambda: random.choice(["学生", "实习生", "刚毕业找工作"]),
            "education": lambda: random.choice(["本科在读", "研究生在读", "本科"]),
            "family_status": lambda: random.choice(["独生子女", "有姐姐", "有弟弟"]),
            "interests": lambda: random.choice(["收集毛绒玩具", "改娃", "追星", "看动漫", "刷小红书"]),
        },
        "facts": {
            ("living", "city"): lambda: random.choice(["杭州", "成都", "南京", "武汉", "西安", "长沙"]),
            ("living", "housing_type"): lambda: random.choice(["住宿舍", "在外租房"]),
            ("relationship", "romantic_status"): lambda: random.choice(["单身", "有对象", "暗恋中"]),
            ("relationship", "social_tendency"): lambda: random.choice(["有点社恐", "i人", "慢热"]),
            ("preference", "favorite_drink"): lambda: random.choice(["奶茶", "柠檬茶", "果茶"]),
            ("hobby", "collection_type"): lambda: random.choice(["Jellycat", "泡泡玛特盲盒", "毛绒玩具", "棉花娃娃"]),
            ("hobby", "art_skill"): lambda: random.choice(["给娃娃换装", "画画", "做手账", "捏OC"]),
            ("hobby", "current_watching"): lambda: random.choice(["动漫", "韩剧", "综艺", "up主视频"]),
            ("pet", "name"): lambda: random.choice(["猫叫奶茶", "狗叫布丁", "仓鼠叫团子", "想养但没养"]),
            ("health", "stress_relief"): lambda: random.choice(["刷小红书", "看B站", "抱玩偶", "和闺蜜聊天"]),
            ("emotion", "long_term_state"): lambda: random.choice(["有点emo", "敏感", "容易被治愈", "需要陪伴"]),
        },
        "categories": ["living", "relationship", "preference", "hobby", "pet", "health", "emotion"],
    },
    "plush_lover_worker": {
        "name": "毛绒控打工人",
        "description": "22-30岁女性，职场新人或小白领，用毛绒玩具治愈自己，喜欢颜值好物",
        "profile": {
            "age": lambda: str(random.randint(22, 30)),
            "gender": lambda: "女",
            "city": lambda: random.choice(["杭州", "成都", "苏州", "南京", "武汉", "长沙", "厦门", "青岛"]),
            "occupation": lambda: random.choice(["设计师", "运营", "新媒体", "行政", "教师", "护士"]),
            "education": lambda: random.choice(["本科", "大专", "硕士"]),
            "family_status": lambda: random.choice(["独生子女", "有兄弟姐妹"]),
            "interests": lambda: random.choice(["收集毛绒玩具", "逛街买好看的东西", "追剧", "拍照打卡"]),
        },
        "facts": {
            ("living", "city"): lambda: random.choice(["杭州", "成都", "苏州", "南京", "武汉", "长沙"]),
            ("living", "housing_type"): lambda: random.choice(["租房", "和朋友合租", "住家里"]),
            ("relationship", "romantic_status"): lambda: random.choice(["单身", "有对象", "刚分手"]),
            ("relationship", "social_tendency"): lambda: random.choice(["有点社恐", "工作外向生活内向", "慢热"]),
            ("work", "work_stress"): lambda: random.choice(["压力挺大", "最近有点累", "还好"]),
            ("preference", "favorite_drink"): lambda: random.choice(["奶茶", "咖啡", "柠檬水"]),
            ("hobby", "collection_type"): lambda: random.choice(["Jellycat", "宜家玩偶", "名创优品玩偶", "棉花娃娃"]),
            ("hobby", "favorite_movie"): lambda: random.choice(["爱情片", "治愈系电影", "动漫电影"]),
            ("pet", "name"): lambda: random.choice(["猫叫年糕", "狗叫麻薯", "养了只猫", "想养猫但租房不让"]),
            ("health", "stress_relief"): lambda: random.choice(["买好看的东西", "吃甜食", "抱玩偶", "刷小红书"]),
            ("emotion", "long_term_state"): lambda: random.choice(["需要被治愈", "容易焦虑", "期待被认同"]),
        },
        "categories": ["living", "work", "relationship", "preference", "hobby", "pet", "health", "emotion"],
    },
    "pet_mom": {
        "name": "宠物铲屎官",
        "description": "20-32岁女性，有猫/狗，把宠物当孩子养，喜欢给毛孩子买东西",
        "profile": {
            "age": lambda: str(random.randint(20, 32)),
            "gender": lambda: "女",
            "city": lambda: random.choice(["杭州", "成都", "深圳", "广州", "南京", "苏州", "厦门"]),
            "occupation": lambda: random.choice(["设计师", "产品经理", "运营", "程序员", "自由职业"]),
            "education": lambda: random.choice(["本科", "硕士"]),
            "family_status": lambda: random.choice(["独生子女", "有兄弟姐妹"]),
            "interests": lambda: random.choice(["撸猫撸狗", "给宠物买东西", "拍宠物视频", "宠物社交"]),
        },
        "facts": {
            ("living", "city"): lambda: random.choice(["杭州", "成都", "深圳", "广州", "南京"]),
            ("living", "housing_type"): lambda: random.choice(["租房", "自己的房子"]),
            ("relationship", "romantic_status"): lambda: random.choice(["单身", "有对象", "已婚"]),
            ("pet", "name"): lambda: random.choice(["猫叫芋圆", "猫叫糯米", "狗叫可乐", "两只猫叫奶茶和布丁"]),
            ("pet", "personality"): lambda: random.choice(["超级粘人", "高冷但傲娇", "调皮捣蛋", "特别乖"]),
            ("preference", "favorite_drink"): lambda: random.choice(["咖啡", "奶茶"]),
            ("hobby", "collection_type"): lambda: random.choice(["宠物用品", "毛绒玩具", "猫咪周边"]),
            ("health", "stress_relief"): lambda: random.choice(["撸猫", "撸狗", "和毛孩子玩"]),
            ("emotion", "long_term_state"): lambda: random.choice(["宠物就是我的精神支柱", "有它们就很治愈"]),
        },
        "categories": ["living", "relationship", "pet", "preference", "hobby", "health", "emotion"],
    },
    "emotional_consumer": {
        "name": "情绪消费玩家",
        "description": "22-34岁女性，为颜值和情绪价值买单，喜欢MBTI/塔罗/星座等",
        "profile": {
            "age": lambda: str(random.randint(22, 34)),
            "gender": lambda: "女",
            "city": lambda: random.choice(["杭州", "成都", "上海", "深圳", "重庆", "长沙", "武汉"]),
            "occupation": lambda: random.choice(["设计师", "新媒体运营", "市场", "HR", "老师", "自由职业"]),
            "education": lambda: random.choice(["本科", "硕士", "大专"]),
            "family_status": lambda: random.choice(["独生子女", "有兄弟姐妹"]),
            "interests": lambda: random.choice(["MBTI社交", "塔罗占卜", "星座运势", "心理测试"]),
        },
        "facts": {
            ("living", "city"): lambda: random.choice(["杭州", "成都", "上海", "深圳", "重庆"]),
            ("living", "housing_type"): lambda: random.choice(["租房", "和朋友合租", "自己的房子"]),
            ("relationship", "romantic_status"): lambda: random.choice(["单身", "有对象", "暧昧中"]),
            ("relationship", "social_tendency"): lambda: random.choice(["INFP", "INFJ", "ENFP", "有点敏感"]),
            ("preference", "favorite_drink"): lambda: random.choice(["奶茶", "咖啡", "气泡水"]),
            ("hobby", "collection_type"): lambda: random.choice(["潘多拉手链", "香薰蜡烛", "塔罗牌", "好看的本子"]),
            ("hobby", "art_skill"): lambda: random.choice(["画画", "做手账", "摄影", "写日记"]),
            ("pet", "name"): lambda: random.choice(["养了猫", "想养宠物", "云吸猫中"]),
            ("health", "stress_relief"): lambda: random.choice(["测塔罗", "看星座运势", "买好看的东西", "和朋友倾诉"]),
            ("emotion", "long_term_state"): lambda: random.choice(["需要被理解", "敏感细腻", "容易共情", "期待被治愈"]),
        },
        "categories": ["living", "relationship", "preference", "hobby", "pet", "health", "emotion"],
    },
    # ===== 原有模板（更新 key 名）=====
    "young_worker": {
        "name": "年轻白领",
        "description": "22-30岁，一线城市工作，租房，单身或恋爱中",
        "profile": {
            "age": lambda: str(random.randint(22, 30)),
            "city": lambda: random.choice(["北京", "上海", "深圳", "杭州", "广州"]),
            "occupation": lambda: random.choice(["程序员", "设计师", "产品经理", "运营", "销售"]),
            "education": lambda: random.choice(["本科", "硕士"]),
            "family_status": lambda: random.choice(["独生子女", "有兄弟姐妹"]),
        },
        "facts": {
            ("living", "housing_type"): lambda: "租房",
            ("relationship", "romantic_status"): lambda: random.choice(["单身", "有对象"]),
            ("work", "work_stress"): lambda: random.choice(["压力挺大", "还好", "比较忙"]),
            ("preference", "favorite_drink"): lambda: random.choice(["咖啡", "奶茶"]),
            ("health", "exercise_frequency"): lambda: random.choice(["偶尔健身", "基本不运动", "每周跑步"]),
        },
        "categories": ["work", "living", "food", "preference", "relationship", "health"],
    },
    "student": {
        "name": "大学生",
        "description": "18-24岁，在校学生，住宿舍或租房",
        "profile": {
            "age": lambda: str(random.randint(18, 24)),
            "city": lambda: random.choice(["北京", "上海", "武汉", "南京", "成都", "西安"]),
            "occupation": lambda: "学生",
            "education": lambda: random.choice(["本科在读", "研究生在读"]),
            "family_status": lambda: random.choice(["独生子女", "有兄弟姐妹"]),
        },
        "facts": {
            ("living", "housing_type"): lambda: random.choice(["住宿舍", "在外租房"]),
            ("relationship", "romantic_status"): lambda: random.choice(["单身", "有对象"]),
            ("work", "work_stress"): lambda: random.choice(["学业压力大", "比较轻松", "考研中"]),
            ("preference", "favorite_drink"): lambda: random.choice(["奶茶", "可乐", "柠檬水"]),
            ("hobby", "favorite_movie"): lambda: random.choice(["动漫", "韩剧", "综艺"]),
        },
        "categories": ["living", "food", "preference", "relationship", "hobby"],
    },
    "new_mom": {
        "name": "新手妈妈",
        "description": "25-35岁，已婚有孩子，关注育儿和家庭",
        "profile": {
            "age": lambda: str(random.randint(25, 35)),
            "city": lambda: random.choice(["北京", "上海", "杭州", "成都", "广州", "深圳"]),
            "occupation": lambda: random.choice(["全职妈妈", "产品经理", "教师", "会计"]),
            "education": lambda: random.choice(["本科", "硕士", "大专"]),
            "family_status": lambda: "有孩子",
        },
        "facts": {
            ("living", "housing_type"): lambda: random.choice(["自己买的房", "和父母住"]),
            ("relationship", "romantic_status"): lambda: "已婚",
            ("family", "child"): lambda: random.choice(["有个1岁的宝宝", "孩子2岁了", "孩子上幼儿园"]),
            ("health", "sleep_habit"): lambda: random.choice(["睡眠不太好", "经常被孩子吵醒"]),
            ("health", "stress_relief"): lambda: random.choice(["刷手机", "追剧", "买东西"]),
        },
        "categories": ["family", "living", "health", "food", "preference"],
    },
    "senior_worker": {
        "name": "职场老人",
        "description": "30-40岁，有一定职场经验，可能已婚",
        "profile": {
            "age": lambda: str(random.randint(30, 40)),
            "city": lambda: random.choice(["北京", "上海", "深圳", "杭州", "广州"]),
            "occupation": lambda: random.choice(["技术总监", "项目经理", "部门主管", "资深工程师"]),
            "education": lambda: random.choice(["本科", "硕士", "博士"]),
            "family_status": lambda: random.choice(["已婚", "有孩子"]),
        },
        "facts": {
            ("living", "housing_type"): lambda: random.choice(["自己买的房", "还在还房贷"]),
            ("relationship", "romantic_status"): lambda: random.choice(["已婚", "有对象"]),
            ("work", "work_stress"): lambda: random.choice(["压力很大", "责任重", "还好习惯了"]),
            ("preference", "favorite_drink"): lambda: random.choice(["咖啡", "茶"]),
            ("health", "exercise_frequency"): lambda: random.choice(["每周健身", "没时间运动", "周末打球"]),
        },
        "categories": ["work", "living", "family", "health", "preference"],
    },
}
