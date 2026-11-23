CHECK_AND_RESERVE = """
-- ARGV: uid, session_id, est, daily_limit, monthly_limit, session_turn_limit,
--       reserve_id, ttl, day_key, month_key, expire_at
local uid = ARGV[1]
local session_id = ARGV[2]
local est = tonumber(ARGV[3])
local daily_limit = tonumber(ARGV[4])
local monthly_limit = tonumber(ARGV[5])
local session_turn_limit = tonumber(ARGV[6])
local reserve_id = ARGV[7]
local ttl = tonumber(ARGV[8])
local day_key = ARGV[9]
local month_key = ARGV[10]
local expire_at = tonumber(ARGV[11])

local daily_used = tonumber(redis.call('GET', day_key) or '0')
local monthly_used = tonumber(redis.call('GET', month_key) or '0')
local turn_key = 'quota:session_turns:' .. session_id
local turns = tonumber(redis.call('GET', turn_key) or '0')

if daily_used + est > daily_limit then
  return {'err', 'quota_daily', tostring(daily_used), tostring(daily_limit)}
end
if monthly_used + est > monthly_limit then
  return {'err', 'quota_monthly', tostring(monthly_used), tostring(monthly_limit)}
end
if turns + 1 > session_turn_limit then
  return {'err', 'quota_session_turns', tostring(turns), tostring(session_turn_limit)}
end

redis.call('INCRBY', day_key, est)
redis.call('EXPIRE', day_key, 172800)
redis.call('INCRBY', month_key, est)
redis.call('EXPIRE', month_key, 2800000)
redis.call('INCR', turn_key)
redis.call('EXPIRE', turn_key, 604800)

local rkey = 'reserve:' .. reserve_id
local grace = ttl + 300
redis.call('HSET', rkey,
  'uid', uid,
  'session_id', session_id,
  'est', tostring(est),
  'status', 'pending',
  'day_key', day_key,
  'month_key', month_key,
  'expire_at', tostring(expire_at)
)
redis.call('EXPIRE', rkey, grace)
redis.call('ZADD', 'reserves:deadline', expire_at, reserve_id)
return {'ok', reserve_id, tostring(daily_limit - daily_used - est)}
"""

COMMIT = """
-- ARGV: reserve_id, actual
local reserve_id = ARGV[1]
local actual = tonumber(ARGV[2])
local rkey = 'reserve:' .. reserve_id
local status = redis.call('HGET', rkey, 'status')
if not status then
  return {'err', 'reserve_not_found'}
end
if status == 'committed' or status == 'released' then
  return {'ok', status, 'idempotent'}
end
local est = tonumber(redis.call('HGET', rkey, 'est') or '0')
local day_key = redis.call('HGET', rkey, 'day_key')
local month_key = redis.call('HGET', rkey, 'month_key')
local refund = est - actual
if refund > 0 then
  redis.call('DECRBY', day_key, refund)
  redis.call('DECRBY', month_key, refund)
elseif refund < 0 then
  local extra = -refund
  redis.call('INCRBY', day_key, extra)
  redis.call('INCRBY', month_key, extra)
end
redis.call('HSET', rkey, 'status', 'committed', 'actual', tostring(actual))
redis.call('ZREM', 'reserves:deadline', reserve_id)
return {'ok', 'committed', tostring(actual)}
"""

RELEASE = """
-- ARGV: reserve_id
local reserve_id = ARGV[1]
local rkey = 'reserve:' .. reserve_id
local status = redis.call('HGET', rkey, 'status')
if not status then
  redis.call('ZREM', 'reserves:deadline', reserve_id)
  return {'err', 'reserve_not_found'}
end
if status == 'committed' or status == 'released' then
  redis.call('ZREM', 'reserves:deadline', reserve_id)
  return {'ok', status, 'idempotent'}
end
local est = tonumber(redis.call('HGET', rkey, 'est') or '0')
local day_key = redis.call('HGET', rkey, 'day_key')
local month_key = redis.call('HGET', rkey, 'month_key')
if est > 0 then
  redis.call('DECRBY', day_key, est)
  redis.call('DECRBY', month_key, est)
end
redis.call('HSET', rkey, 'status', 'released')
redis.call('ZREM', 'reserves:deadline', reserve_id)
return {'ok', 'released', tostring(est)}
"""

SWEEP_EXPIRED = """
-- ARGV: now_unix
local now = tonumber(ARGV[1])
local ids = redis.call('ZRANGEBYSCORE', 'reserves:deadline', '-inf', now)
local released = 0
for i, reserve_id in ipairs(ids) do
  local rkey = 'reserve:' .. reserve_id
  local status = redis.call('HGET', rkey, 'status')
  if status == 'pending' then
    local est = tonumber(redis.call('HGET', rkey, 'est') or '0')
    local day_key = redis.call('HGET', rkey, 'day_key')
    local month_key = redis.call('HGET', rkey, 'month_key')
    if est > 0 and day_key and month_key then
      redis.call('DECRBY', day_key, est)
      redis.call('DECRBY', month_key, est)
    end
    redis.call('HSET', rkey, 'status', 'released', 'swept', '1')
    released = released + 1
  end
  redis.call('ZREM', 'reserves:deadline', reserve_id)
end
return {'ok', tostring(released), tostring(#ids)}
"""
