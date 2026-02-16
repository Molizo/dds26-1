if redis.call("EXISTS", KEYS[2]) == 1 then
    return 0
end

if redis.call("EXISTS", KEYS[1]) == 0 then
    return -2
end

local amount = tonumber(ARGV[1])
if amount == nil or amount < 0 then
    return -1
end

local credit = tonumber(redis.call("HGET", KEYS[1], "credit"))
if credit == nil then
    return -2
end

redis.call("HINCRBY", KEYS[1], "credit", amount)
redis.call("SET", KEYS[2], "1", "EX", tonumber(ARGV[2]))
redis.call("SET", KEYS[3], "1", "EX", tonumber(ARGV[3]))

return 1
