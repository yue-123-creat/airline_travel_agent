-- 浏览用视图：列干净；票价以说明为准（不展示成人票价数字列）
-- 若改列结构：先 DROP VIEW IF EXISTS v_attraction_kb_browse;
CREATE OR REPLACE VIEW v_attraction_kb_browse AS
SELECT
  city_norm                                              AS 城市,
  COALESCE(payload->'content'->>'province', NULLIF(region, city_norm), region)
                                                         AS 省或地区,
  name                                                   AS 景点名,
  id                                                     AS 编号,
  NULLIF(city_raw, city_norm)                            AS 城市原文,
  category                                               AS 类型,
  array_to_string(tags, '、')                            AS 标签,
  COALESCE(
    NULLIF(payload->'visit_info'->'ticket'->>'remark', ''),
    '以景区票价为准'
  )                                                      AS 票价说明,
  left(COALESCE(payload->'visit_info'->>'opening_hours', ''), 40)
                                                         AS 开放时间,
  left(COALESCE(payload->'content'->>'intro', ''), 60)   AS 简介,
  left(COALESCE(payload->'content'->>'tips', ''), 40)    AS 贴士,
  left(COALESCE(payload->'content'->>'best_time', ''), 30)
                                                         AS 最佳季节,
  jsonb_array_length(COALESCE(payload->'relations'->'nearby_spots', '[]'::jsonb))
                                                         AS 邻近数,
  CASE WHEN has_cover THEN '有' ELSE '无' END            AS 有封面,
  CASE WHEN has_intro THEN '有' ELSE '无' END            AS 有简介,
  CASE WHEN lat IS NOT NULL AND lng IS NOT NULL THEN '有' ELSE '无' END
                                                         AS 有坐标,
  updated_at                                             AS 更新时间
FROM attraction_kb;

COMMENT ON VIEW v_attraction_kb_browse IS
  'attraction_kb 人工浏览视图：票价说明统一；ORDER BY 城市, 景点名';
