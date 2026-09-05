import os
import time
import requests
import jpholiday
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# ==========================================
# 設定エリア
# ==========================================

# DiscordのWebhook URLを取得
try:
    from google.colab import userdata
    DISCORD_WEBHOOK_URL = userdata.get('DISCORD_WEBHOOK_URL')
except ImportError:
    # GitHub ActionsのSecrets、または環境変数から取得
    DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# 基準となるURL（ログイン不要版）
BASE_URL = "https://shinagawa.esforta.co.jp/reserve/schedule/1/2"

# ==========================================

def notify_discord(message):
    """Discordにメッセージを通知する関数"""
    if not DISCORD_WEBHOOK_URL:
        print(f"[通知未送信] Webhookが設定されていません: {message}")
        return

    # Discordの文字数制限(2000文字)対策
    if len(message) > 1900:
        message = message[:1900] + "\n\n...（以下略：空き枠が多すぎるため省略されました）"

    headers = {'Content-Type': 'application/json'}
    payload = {
        "content": f"🎾 **品川区プレイコート 空き情報**\n\n{message}"
    }
    
    print(f"--- Discordに以下の内容を送信します ---\n{payload['content']}\n--------------------------------------")
    
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, headers=headers, json=payload)
        response.raise_for_status()
        print("✅ Discordに通知を送信しました。")
    except requests.exceptions.HTTPError as e:
        print(f"❌ Discordへの通知に失敗しました: {e}")
        if e.response is not None:
            print(f"詳細理由: {e.response.text}")
    except Exception as e:
        print(f"❌ 通信エラーが発生しました: {e}")

def get_target_dates():
    """今日から1ヶ月以内の「土日・祝日」の日付リストを取得する"""
    target_dates = []
    today = datetime.now()
    
    # 約1ヶ月(31日)分チェック（当日は予約不可のため、1日後から開始）
    for i in range(1, 32):
        target_date = today + timedelta(days=i)
        
        # weekday() は 5:土曜日, 6:日曜日、または祝日判定
        is_weekend_or_holiday = target_date.weekday() in [5, 6] or jpholiday.is_holiday(target_date.date())
        
        if is_weekend_or_holiday:
            date_str = target_date.strftime("%Y-%m-%d")
            weekday_ja = ["月", "火", "水", "木", "金", "土", "日"]
            
            if jpholiday.is_holiday(target_date.date()):
                holiday_name = jpholiday.is_holiday_name(target_date.date())
                display_str = f"{target_date.strftime('%Y/%m/%d')}(祝/{holiday_name})"
            else:
                display_str = f"{target_date.strftime('%Y/%m/%d')}({weekday_ja[target_date.weekday()]})"
            
            target_dates.append({
                "query_date": date_str,      
                "display_date": display_str  
            })
            
    return target_dates

def check_availability():
    """実際の巡回・スクレイピング処理"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 定期チェックを開始します...")
    target_dates = get_target_dates()
    available_slots = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # 対象日（土日）ごとにURLを開いてチェック
        for target in target_dates:
            url = f"{BASE_URL}?date_from={target['query_date']}"
            print(f"確認中: {target['display_date']} -> {url}")
            
            try:
                page.goto(url)
                page.wait_for_load_state("networkidle")
                time.sleep(1) 
                
                # スクロール処理
                for _ in range(5):
                    page.mouse.wheel(0, 600)
                    time.sleep(0.5)

                # --- 【最重要】空き枠の解析ロジック ---
                time_elements = page.locator("text=/[0-9]{1,2}:[0-9]{2}\\s*-\\s*[0-9]{1,2}:[0-9]{2}/")
                count = time_elements.count()
                
                elements_data = []
                for i in range(count):
                    time_el = time_elements.nth(i)
                    
                    if not time_el.is_visible():
                        continue
                        
                    box = time_el.bounding_box()
                    if not box:
                        continue
                    
                    # JavaScriptを実行し、セルの隅々まで調査する
                    cell_data = time_el.evaluate("""(el) => {
                        let current = el;
                        
                        // 1. 遡りすぎ防止：親要素に「複数の時間枠」が含まれたら、そこを1マスの限界(セル)とする
                        while (current && current.parentElement && current.parentElement.tagName !== 'BODY') {
                            let parent = current.parentElement;
                            let parentText = parent.innerText || "";
                            let timeMatches = parentText.match(/[0-9]{1,2}:[0-9]{2}\\s*-\\s*[0-9]{1,2}:[0-9]{2}/g);
                            if (timeMatches && timeMatches.length > 1) {
                                break; 
                            }
                            current = parent;
                        }
                        
                        let cellText = current.innerText || "";
                        let cellHtml = current.outerHTML || "";
                        let upperHtml = cellHtml.toUpperCase();
                        let upperText = cellText.toUpperCase();
                        let isFull = false;
                        
                        // 2. テキスト・HTMLソースに FULL や 満員 などのキーワードが含まれているか
                        if (upperHtml.includes("FULL") || upperText.includes("FULL") || 
                            upperHtml.includes("ＦＵＬＬ") || upperText.includes("ＦＵＬＬ") ||
                            upperHtml.includes("DISABLED") || upperHtml.includes("RESERVED")) {
                            isFull = true;
                        }
                        
                        // 3. CSSの擬似要素(::before, ::after)で FULL と表示されていないかチェック
                        let allElements = [current, ...current.querySelectorAll('*')];
                        for (let child of allElements) {
                            try {
                                let before = window.getComputedStyle(child, '::before').getPropertyValue('content');
                                let after = window.getComputedStyle(child, '::after').getPropertyValue('content');
                                if (before && before.toUpperCase().includes("FULL")) isFull = true;
                                if (after && after.toUpperCase().includes("FULL")) isFull = true;
                            } catch(e) {}
                        }
                        
                        // 4. そのマスが本当に予約可能な枠か？（プレイコート予約という文字があるか）
                        let hasReserveWord = upperHtml.includes("プレイコート予約") || upperText.includes("プレイコート予約");
                        
                        return { isFull: isFull, hasReserveWord: hasReserveWord };
                    }""")
                    
                    time_text = time_el.inner_text().strip()
                    elements_data.append({
                        "x": box["x"] + (box["width"] / 2),
                        "text": time_text,
                        "isFull": cell_data["isFull"],
                        "hasReserveWord": cell_data["hasReserveWord"]
                    })
                
                if not elements_data:
                    continue
                    
                # 列（コート）の特定
                x_coords = sorted([d["x"] for d in elements_data])
                clusters = []
                for x in x_coords:
                    if not clusters:
                        clusters.append([x])
                    else:
                        if x - sum(clusters[-1])/len(clusters[-1]) < 30:
                            clusters[-1].append(x)
                        else:
                            clusters.append([x])
                
                cluster_centers = [sum(c)/len(c) for c in clusters]
                
                # 空き枠の判定と通知リストへの追加
                for d in elements_data:
                    x = d["x"]
                    col_idx = -1
                    for idx, center in enumerate(cluster_centers):
                        if abs(x - center) < 30:
                            col_idx = idx
                            break
                    
                    if col_idx == 0:
                        court_name = "プレイコート１"
                    elif col_idx == 1:
                        court_name = "プレイコート２"
                    else:
                        continue
                        
                    # ★ セルの中に「FULL」要素が一切なく、かつ「プレイコート予約」が含まれていれば真の空き枠
                    if not d["isFull"] and d["hasReserveWord"]:
                        found_msg = f"・ {target['display_date']} {d['text']} ({court_name})"
                        if found_msg not in available_slots:
                            available_slots.append(found_msg)
                            
            except Exception as e:
                print(f"ページ {url} の確認中にエラーが発生しました: {e}")
            
            time.sleep(2)

        browser.close()

    if available_slots:
        message = "以下の日時に空きが見つかりました！\n\n"
        for slot in available_slots:
            message += f"{slot}\n"
        
        notify_discord(message)
    else:
        print("今回は空き枠が見つかりませんでした。")
        
    print("チェックが完了しました。\n")

def main():
    print("🎾 品川区プレイコート 空き通知アプリを起動しました。")
    print("空き状況のチェックを1回実行します。")
    
    # 無限ループとスケジューラを削除し、1回だけ実行して終了するように変更
    check_availability()

if __name__ == "__main__":
    main()