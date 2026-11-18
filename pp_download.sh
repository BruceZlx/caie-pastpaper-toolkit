#!/bin/bash
# 单个 PDF 下载脚本（配合 xargs 并行使用）
# 用法: pp_download.sh <pdf直链URL> <目标根目录>
#   例: pp_download.sh https://pastpapers.papacambridge.com/directories/CAIE/CAIE-pastpapers/upload/9702_w24_qp_11.pdf "$PWD/9702（物理）"
# 从文件名自动解析 科目/年份/会话 并归档为 <根目录>/<年份>/<会话>/<原文件名>
# 自带 5 次重试 + %PDF 魔数校验，成功输出 "OK 文件名"，失败输出 "FAIL 文件名"
url="$1"
base="$2"
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

fname=$(basename "$url")                 # e.g. 9702_w24_qp_11.pdf
stem="${fname%.pdf}"                     # 9702_w24_qp_11
code=$(echo "$stem" | cut -d_ -f2)       # w24
seas="${code:0:1}"; yr="20${code:1:2}"
case "$seas" in
  m) sess="Feb-Mar" ;;
  s) sess="May-Jun" ;;
  w) sess="Oct-Nov" ;;
  *) sess="unknown" ;;
esac
dir="$base/$yr/$sess"
mkdir -p "$dir"
out="$dir/$fname"

for attempt in 1 2 3 4 5; do
  curl -s -L --fail --max-time 120 --retry 2 -H "User-Agent: $UA" "$url" -o "$out" && break
  sleep $((attempt * 2))
done

if [ -s "$out" ] && head -c 4 "$out" | grep -q "%PDF"; then
  echo "OK $fname"
else
  echo "FAIL $fname"
  rm -f "$out"
fi
