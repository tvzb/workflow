import sys
import base64
import requests
import time

def test_speed(url):
    try:
        start_time = time.time()
        # 仅请求头部，设置3秒超时避免 Actions 运行超时
        response = requests.get(url, timeout=3, stream=True)
        if response.status_code == 200:
            return round((time.time() - start_time) * 1000) 
    except:
        pass
    return 9999 

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("未接收到数据")
        sys.exit(1)
        
    encoded_data = sys.argv[1]
    raw_text = base64.b64decode(encoded_data).decode('utf-8')
    lines = raw_text.strip().split('\n')
    
    ordered_items = []
    current_channel_name = None
    current_channel_links = []
    seen_urls = set()
    
    print("开始测速、去重与同频道测速排序...")
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # 如果是分类标签 (例如：央视频道,#genre#)
        if '#genre#' in line:
            # 遇到新标签前，先保存上一组累积的频道
            if current_channel_name and current_channel_links:
                ordered_items.append({"type": "channel", "name": current_channel_name, "links": current_channel_links})
                current_channel_name = None
                current_channel_links = []
            
            ordered_items.append({"type": "category", "name": line})
            continue
            
        # 如果是具体直播源
        if ',' in line:
            name, url = line.split(',', 1)
            name = name.strip()
            url = url.strip()
            
            # 去重逻辑
            if url in seen_urls:
                continue
            seen_urls.add(url)
            
            speed = test_speed(url)
            
            if speed < 9999:
                print(f"✅ {name} 速度: {speed}ms")
                # 只要频道名不同，说明进入了下一个频道
                if name != current_channel_name:
                    # 先保存上一组频道
                    if current_channel_name and current_channel_links:
                        ordered_items.append({"type": "channel", "name": current_channel_name, "links": current_channel_links})
                    # 开启新频道的记录
                    current_channel_name = name
                    current_channel_links = [(url, speed)]
                else:
                    # 相同频道名，继续追加链接
                    current_channel_links.append((url, speed))
            else:
                print(f"❌ {name} 失效或超时，已删除")

    # 循环结束，把最后一组还没保存的保存进去
    if current_channel_name and current_channel_links:
        ordered_items.append({"type": "channel", "name": current_channel_name, "links": current_channel_links})

    # 写入最终的 iptv.txt
    with open('iptv.txt', 'w', encoding='utf-8') as f:
        first_category = True
        for item in ordered_items:
            if item.get("type") == "category":
                # 分类标签前加一个空行，保持排版美观（第一个标签除外）
                if not first_category:
                    f.write('\n')
                f.write(item["name"] + '\n')
                first_category = False
            elif item.get("type") == "channel":
                name = item["name"]
                links = item["links"]
                # 核心要求：同一个频道名内部，按速度由快到慢（升序）排列
                links.sort(key=lambda x: x[1])
                for url, speed in links:
                    f.write(f"{name},{url}\n")
                
    print("生成完毕，已输出为按同频道排序的标准格式 iptv.txt")
