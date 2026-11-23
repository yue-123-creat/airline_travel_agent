-- 温州机场（WNZ）airport_services 入库脚本
-- 来源：温州机场.xlsx
-- details：Excel 原文包装为 {"text": "..."}；空/— 则为 {}
-- location：保留 TEXT；空/— 则为 NULL
-- 分类：transport/navigation/checkin/facility/lounge/emergency
-- rag_links.guide_chunk_ids 先空；原文档切块入库后再回填

DELETE FROM airport_services WHERE airport_code = 'WNZ';

INSERT INTO airport_services (
    airport_code, service_category, service_name, details, location, rag_links
) VALUES
    ('WNZ', 'transport', '机场巴士（汽车南站线）', '{"text": "票价15元/人，车上扫码购票；热线13757708208（09:00-20:00）"}'::jsonb, '上客点：T2到达层楼外1号门外；停靠华瓯佳园、温州广播电视中心、新城汽车站、中西结合医院、喜来登（时代广场）、桥儿头、汽车南站', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'transport', '网约车等候区', '{"text": "1-5号滴滴上客点，6-9号高德上客点"}'::jsonb, '国内到达出口乘扶梯/直梯至负一层，经S1、S2通道到环游里购物中心入口后向左，按指示牌前往', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'transport', '出租车', '{"text": "监督投诉电话12328"}'::jsonb, 'T2到达层4号门外', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'transport', '私家车与停车场', '{"text": "接送客首选综合交通中心；P2供网约车及旅客；P6供T1接送客"}'::jsonb, '综合交通中心停车楼（A/C区，T2前西面）；P2（T2西南侧/航二路南侧）；P6（T1西侧）', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'transport', 'P2网约车停车场', '{"text": "网约车前15分钟免费，其后按小时计费；优惠办理P2出口收费岗亭（08:00-20:00）"}'::jsonb, 'T2东侧/西南侧约300米', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'transport', '综合交通中心停车场', '{"text": "A区664车位、C区674车位，限高2.1米；可经空港二路地面/高架进入"}'::jsonb, 'T2航站楼前西面', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'transport', 'P6国际临时停车场', '{"text": "约100车位，主要供T1接送客"}'::jsonb, 'T1航站楼西侧', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'transport', '轨道交通S1线（机场站）', '{"text": "往桐岭首班6:30、末班22:44（往动车南末班23:15）；往双瓯大道首班6:33、末班23:21"}'::jsonb, '市域铁路S1线机场站', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'transport', '轨道交通S2线（机场站）', '{"text": "往清东路首班6:29、末班23:09；往东山首班6:25、末班23:17"}'::jsonb, 'S2线机场站', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'transport', '租车（神州租车）', '{"text": "营业09:00-18:00；电话13276879170"}'::jsonb, 'T2一楼1号门附近', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'transport', '市区班线/地面班线咨询', '{"text": "班线时刻13757708208（09:00-20:00）；售票柜台0577-86892971（08:30-17:00）"}'::jsonb, NULL, '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'navigation', '国内出发乘机路线', '{"text": "建议起飞前2小时到达；登机口起飞前15分钟关闭"}'::jsonb, '出发大厅→值机/托运→安检→隔离区登机口', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'navigation', '国内到达路线', '{"text": "行李查询0577-96555-3"}'::jsonb, '到达层行李转盘提取行李后，从到达层出口离开', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'navigation', '中转服务', '{"text": "08:30至当天出港中转结束；同日国内中转间隔≥60分钟；优先选座、免行李及人身安检；急转预约0577-96555；远机位及九元航空暂有限制"}'::jsonb, '中转柜台', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'navigation', '综合服务/问询柜台', '{"text": "电话0577-96555；提供进出港航班、办票及特殊旅客咨询"}'::jsonb, 'T2航站楼问询处', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'navigation', '机场热线电话', '{"text": "总机/问询0577-96555；航班查询-1、货单-2、行李-3、特殊旅客-8、投诉-9、人工-0；邮箱1045718717@qq.com"}'::jsonb, NULL, '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'navigation', '边防检查指引', '{"text": "交验有效证件/签证，出境另出示登机牌；咨询12367"}'::jsonb, '国际/港澳台边检柜台', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'navigation', '海关检查指引', '{"text": "申报物品走红色通道，其余绿色通道；咨询12360；驻场0577-88012564"}'::jsonb, '海关申报通道（红/绿）', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'navigation', '卫生检疫指引', '{"text": "重大疫情填报健康申明卡；携带物过X光机"}'::jsonb, '检验检疫专用通道', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '值机时间', '{"text": "一般起飞前4小时开始、前35分钟截止（快线除外）；九元/春秋前100分钟；建议提前2小时到场"}'::jsonb, '值机柜台/自助值机', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '值机方式', '{"text": "凭有效证件办理；无托运行李可用二维码电子登机牌过检"}'::jsonb, '相应航司柜台或自助值机柜台', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '值机柜台分布', '{"text": "B11加急、B12特殊旅客、B13-14两舱；C01-06自助托运、C07贵宾；D09-10值班主任/特殊/加急、D11-12两舱；以航站楼公布为准"}'::jsonb, 'T2出发大厅B/C/D岛', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '特殊旅客柜台', '{"text": "军人、消防、医护、无陪儿童、老年、孕妇、携婴、病残/轮椅、首乘等"}'::jsonb, 'B12（CA/ZH/SC/大连航在D09-D10）', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '加急旅客柜台', '{"text": "办理起飞前45-35分钟的加急旅客"}'::jsonb, 'B11', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '自助行李托运', '{"text": "开放05:30-20:00；办理时段起飞前4小时至1小时；暂不支持CA/ZH/SC/AQ/9C/KN、联程及超重缴费旅客"}'::jsonb, 'C01-C06自助托运柜台', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '人工行李托运', '{"text": "凭小程序或机场指示牌确认对应办票柜台"}'::jsonb, '航司人工托运柜台', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '临时乘机证明', '{"text": "电子版：微信/支付宝搜“民航临时乘机证明”"}'::jsonb, '自助机：T2出发层警务室门口；人工：T2出发层警务室', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '安检流程与通道', '{"text": "过检前备好证件与登机牌，取出充电宝、雨伞、电脑等"}'::jsonb, 'T2安检待检区（1-20号通道）', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '爱心/优先安检通道', '{"text": "爱心、头等/公务、军人依法优先、消防员优先"}'::jsonb, '19号通道（待检区右前方）', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '女性专用安检通道', '{}'::jsonb, '10号通道（待检区左前方）', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '易安检专用通道', '{"text": "起飞前48小时内支付宝搜“民航公安服务”实名预约"}'::jsonb, '4号通道（待检区左侧）', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '行李限制（随身/托运）', '{"text": "随身经济/公务1件、头等2件，单件≤20×40×55cm且≤5kg；免费托运额头等40/公务30/经济20kg；锂电池、充电宝不得托运；细则以航司为准"}'::jsonb, NULL, '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'checkin', '联检（边防/海关/检疫）', '{"text": "边检12367、海关12360"}'::jsonb, '国际进出港联检区', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'facility', '母婴室', '{"text": "关门保护隐私，勿独留婴幼儿，用毕及时离开"}'::jsonb, '航站楼内母婴室', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'facility', '卫生间', '{}'::jsonb, '航站楼各区域', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'facility', '饮用水', '{"text": "提供常温/热水及一次性纸杯"}'::jsonb, 'T1共9台、T2共22台直饮水机', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'facility', '中图便利店', '{"text": "06:00-22:00（到达层至24:00）；面包零食、香烟、彩票"}'::jsonb, '安检内207-209登机口对面；到达层1号门对面', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'facility', '行李寄存', '{"text": "06:00-22:00；4小时内5元/件，4-12小时15元，12-24小时25元，累计计费；电话0577-85807459"}'::jsonb, 'T2一楼到达层4号门对面', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'facility', '行李打包', '{"text": "按箱包、海鲜泡沫箱、锐器/钝器/工具等分类包装；禁运品不得夹带"}'::jsonb, '出发厅行李打包处', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'facility', '行李快递', '{"text": "顺丰08:30-18:00；自助柜05:00至出港结束；T1暂无柜台，下单后顺丰上门取件"}'::jsonb, 'T2顺丰柜台：到达层4号门对面失物招领旁；自助快递柜：安检通道后端', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'facility', '医疗急救', '{"text": "0577-86892120 / 85807971 / 18205871600"}'::jsonb, '急救室1：T2到达大厅（一层）隔离区北侧；急救室2：T2出发大厅（三层）隔离区南侧', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'facility', 'WiFi', '{"text": "连接“Airport Free Wifi”，一键认证后上网"}'::jsonb, '航站楼内', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'facility', '临时休息/按摩椅', '{"text": "可休息、按摩、充电"}'::jsonb, '刻睡舱：212/216/203登机口旁；按摩椅：到达层4号门对面、安检内224/204登机口对面', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'facility', '货币兑换', '{"text": "电话0577-89783689；可单向兑换美元、欧元、港币等18种货币"}'::jsonb, 'T1国际航站楼迎送厅（3号门区域）', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'facility', '普通退税', '{"text": "凭有效证件、购物发票及海关签章《离境退税申请单》办理"}'::jsonb, '退税代理点', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'lounge', '头等舱/贵宾休息室（隔离区内）', '{"text": "两舱/金银卡按会员规则使用；散客成人160元、儿童80元"}'::jsonb, 'T1国际19号登机口对面；T2：F1在213对面、F2在214对面、F3东航近F2、F4国航与东航相邻', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'lounge', '机场贵宾厅', '{"text": "预约0577-86898888（06:30-22:00）；至少起飞前60分钟到厅办托运；驾车进贵宾区需预约车牌"}'::jsonb, 'T2航站楼南端', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'lounge', '贵宾服务热线', '{"text": "0577-86898888；服务06:30-21:30（预约中心至22:00）"}'::jsonb, NULL, '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'lounge', '航空售票处', '{"text": "营业约06:30-20:00；机票销售"}'::jsonb, 'T2出发层1号门（凯达、莱特）', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'lounge', '餐饮服务（到达层）', '{"text": "Daing Cafe雀巢、金饭碗、一鸣真鲜奶吧"}'::jsonb, 'T2到达层1号门旁/到达1号门', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'lounge', '餐饮服务（出发层安检外）', '{"text": "肯德基、王元兴；普奇咖啡、浙江名小吃"}'::jsonb, '出发1号门斜对面、出发4号门斜对面', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'lounge', '餐饮服务（出发层安检内）', '{"text": "瑞幸、东池便当、星巴克、一鸣、书亦、十八家面馆、霸王茶姬、品品香、富春茶社"}'::jsonb, '212/213-214/204/216/224登机口附近', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'lounge', '商业购物', '{"text": "服装箱包、美妆、茶礼、文创书店、伴手礼等（LATOALPS 4号安检口对面、奥康、比音勒芬、飞旅购、垂直之光书店、浙江伴手礼等）"}'::jsonb, '安检内外各商铺点位', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'emergency', '航班延误保障', '{"text": "关注航班获取发餐、住宿等航延动态"}'::jsonb, '温州机场公安小程序', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'emergency', '退改签', '{"text": "在哪里购买在哪里退改签；机场客服24小时0577-96555"}'::jsonb, '购票渠道（航司/平台）', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'emergency', '失物招领', '{"text": "06:00-22:00；电话0577-85807459；小程序“温州机场失物招领”；证件类交警务室0577-85807110"}'::jsonb, 'T2一楼到达层4号门对面', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'emergency', '行李查询', '{"text": "行查0577-85807469；到达行李0577-96555-3；CA/ZH/SC 0577-89721159/89721160"}'::jsonb, NULL, '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'emergency', '投诉与建议', '{"text": "机场投诉0577-96555-9；邮箱1045718717@qq.com；民航监督12326"}'::jsonb, NULL, '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'emergency', '老人协助服务', '{"text": "05:10至出港结束；电话0577-96555；面向60岁以上无人陪伴且需协助老人"}'::jsonb, 'T2问询处', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'emergency', '无陪儿童服务', '{"text": "提前向航司申请；当天至少提前120分钟到场；电话0577-96555"}'::jsonb, '国内T2问询处、国际T1问询处（CA/ZH/SC除外）', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'emergency', '轮椅服务', '{"text": "提前向航司申请；提供登记与引领；当天至少提前120分钟到场"}'::jsonb, '国内T2问询处、国际T1问询处（CA/ZH/SC除外）', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'emergency', '无陪老人服务', '{"text": "航司确认并预约；当天至少提前120分钟到场；电话0577-96555"}'::jsonb, '国内T2/国际T1问询处（CA/ZH/SC除外）', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'emergency', '首乘旅客服务', '{"text": "05:10至出港结束；手续办理、值机选座、登机引导；电话0577-96555"}'::jsonb, 'T2问询处', '{"guide_chunk_ids": []}'::jsonb),
    ('WNZ', 'emergency', '异地共享/无忧轮椅服务', '{"text": "异地共享预约0577-96555（24小时）；无忧轮椅08:30-22:00"}'::jsonb, 'T2问询柜台；覆盖机场、温州南站及S1各站', '{"guide_chunk_ids": []}'::jsonb);

-- 合计 64 条
--   transport: 11
--   navigation: 8
--   checkin: 14
--   facility: 12
--   lounge: 8
--   emergency: 11

-- 筛选示例：
-- SELECT service_name, location, details FROM airport_services WHERE airport_code='WNZ' AND service_category='transport';
