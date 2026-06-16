import json
import os
import logging
import asyncio
import hashlib
import urllib.parse
import re
import tempfile
import threading
from collections import OrderedDict
from typing import Dict, Any, Optional, List
from uuid import uuid4
from datetime import datetime
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# ================== 配置区域 ==================
TOKEN = "8824336933:AAGnmJu0jM_9dusBiHoKs0YypagDXw0zNj0"
ADMIN_USER_ID = 7002638062
WELCOME_CHAT_IDS = [ADMIN_USER_ID]

# ================== OkayPay API 配置 ==================
API_URL = 'https://api.okaypay.me/shop/'
shop_id = "34543"
shop_token = "8fkGUXg5BszGHK1MPb3SFhWpYLt2Jwa"
NAME = "商品购买"
bot_username = "XIAOCIVBBOT"

# 数据文件路径
BALANCE_FILE = "user_balances.json"
ORDER_FILE = "orders.json"
PRODUCTS_FILE = "products.json"
COUNTRIES_FILE = "countries.json"
CARD_FILE = "cards.json"
CATEGORIES_FILE = "categories.json"
SENT_WELCOME_FILE = "sent_welcome.json"
RECHARGE_ORDERS_FILE = "recharge_orders.json"

# ================== 固定分类（使用ID映射） ==================
FIXED_CATEGORIES = {
    "cat_baozihao": " 各国豹子号",
    "cat_huanbang": " 各国换绑注册",
    "cat_jiechi": " 各国劫持账号",
    "cat_shuangxiang": " 各国双向账号"
}

# 菜单按钮列表（防止误保存）
MENU_BUTTONS = [
    "😡 自助购买",
    "💰 我的余额",
    "💎 充值余额",
    "📋 购买记录",
    "📞 联系客服",
    "⚙️ 管理面板"
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================== OkayPay API 函数 ==================
def _sign(data: dict) -> dict:
    data['id'] = shop_id
    data = {k: v for k, v in data.items() if v or v == 0}
    data = OrderedDict(sorted(data.items()))
    query = urllib.parse.urlencode(data, quote_via=urllib.parse.quote)
    query = urllib.parse.unquote(query)
    data['sign'] = hashlib.md5(
        (query + '&token=' + shop_token).encode()
    ).hexdigest().upper()
    return data

def _post(endpoint: str, data: dict) -> dict:
    data = _sign(data)
    try:
        resp = requests.post(API_URL + endpoint, data=data, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {'code': -1, 'msg': str(e)}

def okpay_create_deposit(order_number: str, amount: float, user_id: str) -> dict:
    return _post('payLink', {
        'unique_id': order_number,
        'name': f'{NAME}充值',
        'amount': str(amount),
        'return_url': f'https://t.me/{bot_username}',
        'coin': 'USDT',
    })

def okpay_check_deposit(unique_id: str) -> dict:
    return _post('checkDeposit', {'unique_id': unique_id})

def okpay_balance() -> dict:
    return _post('balance', {})

# ================== Markdown 转义工具 ==================
def escape_markdown(text: str) -> str:
    """转义 Markdown 特殊字符，防止解析错误"""
    if not text:
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(['\\' + char if char in escape_chars else char for char in str(text)])

# ================== 数据持久化（原子写入） ==================
def load_json(file_path: str, default: Any = None) -> Any:
    if default is None:
        default = {}
    if not os.path.exists(file_path):
        return default
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            if not content.strip():
                return default
            return json.loads(content)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"加载文件失败 {file_path}: {e}")
        backup_file = file_path + ".backup"
        if os.path.exists(backup_file):
            try:
                with open(backup_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                pass
        return default

def save_json(file_path: str, data: Any) -> None:
    """原子写入：先写临时文件，再重命名"""
    try:
        fd, temp_path = tempfile.mkstemp(
            suffix='.json',
            prefix='tmp_',
            dir=os.path.dirname(file_path) or '.'
        )
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        
        if os.path.exists(file_path):
            try:
                os.replace(file_path, file_path + ".backup")
            except:
                pass
        
        os.replace(temp_path, file_path)
        
    except Exception as e:
        logger.error(f"保存文件失败 {file_path}: {e}")
        raise

# 初始化数据
user_balances: Dict[str, float] = load_json(BALANCE_FILE)
orders: Dict[str, Dict] = load_json(ORDER_FILE)
products: Dict[str, Dict] = load_json(PRODUCTS_FILE, {})
countries: Dict[str, Dict] = load_json(COUNTRIES_FILE, {})
cards: Dict[str, List[Dict]] = load_json(CARD_FILE, {})
categories: Dict[str, str] = load_json(CATEGORIES_FILE, {})

# 确保固定分类存在
for cat_id, cat_name in FIXED_CATEGORIES.items():
    if cat_id not in categories:
        categories[cat_id] = cat_name
    else:
        categories[cat_id] = cat_name

save_json(CATEGORIES_FILE, categories)

if products is None:
    products = {}
    save_json(PRODUCTS_FILE, products)
if cards is None:
    cards = {}
    save_json(CARD_FILE, cards)

sent_welcome: Dict[str, bool] = load_json(SENT_WELCOME_FILE, {})
recharge_orders: Dict[str, Dict] = load_json(RECHARGE_ORDERS_FILE, {})

# ================== 并发安全锁 ==================
card_lock = threading.Lock()
balance_lock = threading.Lock()

def save_all_data() -> None:
    save_json(BALANCE_FILE, user_balances)
    save_json(ORDER_FILE, orders)
    save_json(PRODUCTS_FILE, products)
    save_json(COUNTRIES_FILE, countries)
    save_json(CARD_FILE, cards)
    save_json(CATEGORIES_FILE, categories)
    save_json(SENT_WELCOME_FILE, sent_welcome)
    save_json(RECHARGE_ORDERS_FILE, recharge_orders)

# ================== 辅助函数 ==================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID

def get_available_card(product_key: str) -> Optional[str]:
    """线程安全获取卡密"""
    with card_lock:
        if product_key not in cards:
            return None
        for i, card_info in enumerate(cards[product_key]):
            if not card_info.get("used", False):
                if not cards[product_key][i].get("used", False):
                    cards[product_key][i]["used"] = True
                    save_json(CARD_FILE, cards)
                    return card_info["card"]
    return None

def add_cards_bulk(product_key: str, card_list: List[str]) -> int:
    """批量添加卡密 - 过滤菜单按钮"""
    if product_key not in cards:
        cards[product_key] = []
    added = 0
    for card in card_list:
        card = card.strip()
        if card and card not in MENU_BUTTONS:
            cards[product_key].append({"card": card, "used": False})
            added += 1
    save_json(CARD_FILE, cards)
    return added

def get_product_stock(product_key: str) -> int:
    """获取商品库存"""
    if product_key not in cards:
        return 0
    with card_lock:
        return len([c for c in cards[product_key] if not c.get('used', False)])

def safe_product_get(product_key: str) -> Optional[Dict]:
    """安全获取商品信息"""
    return products.get(product_key)

def create_order(user_id: str, product_key: str, product_name: str, price: float, delivery_data: str) -> str:
    order_id = str(uuid4())[:8]
    orders[order_id] = {
        "user_id": user_id,
        "product_key": product_key,
        "product_name": product_name,
        "price_usdt": price,
        "total_usdt": price,
        "status": "completed",
        "delivery_data": delivery_data,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    save_all_data()
    return order_id

def create_recharge_order(user_id: str, amount: float) -> str:
    order_id = f"R{datetime.now().strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
    recharge_orders[order_id] = {
        "user_id": user_id,
        "amount": amount,
        "status": "pending",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    save_all_data()
    return order_id

def confirm_recharge(order_id: str, tx_id: str = None) -> bool:
    if order_id not in recharge_orders:
        return False
    order = recharge_orders[order_id]
    if order["status"] == "completed":
        return True
    user_id = order["user_id"]
    amount = order["amount"]
    with balance_lock:
        user_balances[user_id] = user_balances.get(user_id, 0.0) + amount
    order["status"] = "completed"
    order["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if tx_id:
        order["tx_id"] = tx_id
    save_all_data()
    logger.info(f"用户 {user_id} 充值成功 +{amount} USDT")
    return True

async def check_pending_recharges(context: ContextTypes.DEFAULT_TYPE) -> None:
    pending_orders = [oid for oid, o in recharge_orders.items() if o.get("status") == "pending"]
    for order_id in pending_orders:
        result = okpay_check_deposit(order_id)
        if result.get('code') == 200:
            data = result.get('data', {})
            if data.get('status') == 1:
                if confirm_recharge(order_id, data.get('tx_id')):
                    order = recharge_orders[order_id]
                    try:
                        await context.bot.send_message(
                            chat_id=int(order["user_id"]),
                            text=f"✅ *充值成功！*\n\n金额：{order['amount']} USDT\n当前余额：`{user_balances[order['user_id']]:.4f} USDT`",
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.error(f"通知用户失败: {e}")

async def get_shop_name(context: ContextTypes.DEFAULT_TYPE) -> str:
    try:
        admin_user = await context.bot.get_chat(ADMIN_USER_ID)
        admin_name = admin_user.full_name or admin_user.username or "管理员"
        return f"🎫 {escape_markdown(admin_name)}の自助卖号"
    except:
        return "🎫 自助卖号机器人"

# ================== 欢迎消息 ==================
def get_welcome_message(admin_name: str) -> str:
    safe_name = escape_markdown(admin_name)
    return (
        f"🌈欢迎光临{safe_name}自助卖号机器人 \n\n"
        "✅TG账号自助购买 \n\n"
        "1、请先少量购买测试，合适可继续购买\n\n"
        "2、购买后第一时间检测是否死号，如帐号有问题请十分钟内联系我处理，包售后，超时不售后\n\n"
        "3、群发群、拉人还是私信都有技巧，不能盲目，可以进群交流\n"
        "——————————————\n\n"
        "🛰️【频道】 https://t.me/APl57\n"
        "👥【群组】 https://t.me/ahdgsv\n"
        "☎️【客服】 @APl520\n"
        "🛠️【工具】 反登录机器人:@vzbbjkbot 轮训机器人:@cynsantanametgalabot\n"
        "🌐【零售】 https://buy.wlqfk.net/shop/41WFDSM2\n\n"
        "⚙ /start   ⬅点击命令打开底部菜单\n\n"
        "机器人支持USDT 人民币充值 不接受使用后售后"
    )

# ================== 键盘构建 ==================
async def get_main_menu_keyboard(context: ContextTypes.DEFAULT_TYPE, is_admin_user: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📦 自助购买", callback_data="product_list")],
        [InlineKeyboardButton("💰 我的余额", callback_data="my_balance")],
        [InlineKeyboardButton("💎 充值余额", callback_data="recharge_balance")],
        [InlineKeyboardButton("📋 购买记录", callback_data="my_orders")],
        [InlineKeyboardButton("👤 联系客服", callback_data="contact_admin")],
    ]
    if is_admin_user:
        buttons.append([InlineKeyboardButton("⚙️ 管理面板", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

def get_main_reply_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton("📦 自助购买"), KeyboardButton("💰 我的余额")],
        [KeyboardButton("💎 充值余额"), KeyboardButton("📋 购买记录")],
        [KeyboardButton("📞 联系客服")]
    ]
    if is_admin:
        buttons.append([KeyboardButton("⚙️ 管理面板")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def get_product_categories_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    """动态分类键盘 - 加号用于添加新分类"""
    buttons = []
    
    for cat_id, cat_name in categories.items():
        total_stock = 0
        for key, prod in products.items():
            if prod.get('category_id', '') == cat_id:
                total_stock += get_product_stock(key)
        
        stock_text = f" (库存:{total_stock})" if total_stock > 0 else ""
        
        if is_admin:
            buttons.append([
                InlineKeyboardButton(f"📁 {cat_name}{stock_text}", callback_data=f"cat_{cat_id}"),
                InlineKeyboardButton("➕", callback_data="add_category")  # ✅ 点击加号 = 添加新分类
            ])
        else:
            buttons.append([InlineKeyboardButton(f"📁 {cat_name}{stock_text}", callback_data=f"cat_{cat_id}")])

    # 管理员底部快捷入口
    if is_admin:
        buttons.append([InlineKeyboardButton("📁 管理分类", callback_data="manage_categories")])

    buttons.append([InlineKeyboardButton("🏠 主菜单", callback_data="main_menu")])
    return InlineKeyboardMarkup(buttons)

def get_products_by_category(category_id: str, is_admin: bool = False) -> InlineKeyboardMarkup:
    """分类下的商品列表"""
    buttons = []
    category_name = categories.get(category_id, "未知分类")

    for key, prod in products.items():
        if prod.get('category_id', '') == category_id:
            stock = get_product_stock(key)
            safe_name = escape_markdown(prod.get('name', '未知'))

            if is_admin:
                buttons.append([InlineKeyboardButton(
                    f"⚙️ {safe_name} - {prod.get('price_usdt', 0)} USDT (库存:{stock})",
                    callback_data=f"admin_manage_{key}"
                )])
            else:
                if stock > 0:
                    buttons.append([InlineKeyboardButton(
                        f"📦 {safe_name} - {prod.get('price_usdt', 0)} USDT",
                        callback_data=f"view_product_{key}"
                    )])
                else:
                    buttons.append([InlineKeyboardButton(
                        f"❌ {safe_name} - 已售罄",
                        callback_data="noop"
                    )])

    if not buttons:
        if is_admin:
            buttons.append([InlineKeyboardButton("➕ 添加商品到此分类", callback_data=f"add_product_to_{category_id}")])
        else:
            buttons.append([InlineKeyboardButton("📁 暂无商品", callback_data="noop")])

    buttons.append([InlineKeyboardButton("🔙 返回分类列表", callback_data="product_list")])
    return InlineKeyboardMarkup(buttons)

def get_product_detail_keyboard(product_key: str, category_id: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("💎 立即购买", callback_data=f"user_buy_{product_key}")],
        [InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{category_id}")],
        [InlineKeyboardButton("🏠 主菜单", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(buttons)

def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📊 查看统计", callback_data="admin_stats")],
        [InlineKeyboardButton("📋 所有订单", callback_data="admin_orders")],
        [InlineKeyboardButton("💰 充值记录", callback_data="admin_recharge_records")],
        [InlineKeyboardButton("💎 商户余额", callback_data="admin_balance")],
        [InlineKeyboardButton("📁 管理分类", callback_data="manage_categories")],
        [InlineKeyboardButton("🔄 刷新数据", callback_data="refresh_data")],
        [InlineKeyboardButton("🔧 修复数据", callback_data="fix_data")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(buttons)

def get_product_action_keyboard(product_key: str, category_id: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("💰 修改价格", callback_data=f"change_price_{product_key}")],
        [InlineKeyboardButton("📝 修改描述", callback_data=f"change_desc_{product_key}")],
        [InlineKeyboardButton("📦 添加卡密", callback_data=f"add_stock_{product_key}")],
        [InlineKeyboardButton("📋 查看卡密", callback_data=f"view_stock_{product_key}")],
        [InlineKeyboardButton("✏️ 重命名", callback_data=f"rename_product_{product_key}")],
        [InlineKeyboardButton("🗑️ 删除商品", callback_data=f"delete_product_{product_key}")],
        [InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{category_id}")]
    ]
    return InlineKeyboardMarkup(buttons)

def get_recharge_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("1 USDT", callback_data="recharge_1")],
        [InlineKeyboardButton("5 USDT", callback_data="recharge_5")],
        [InlineKeyboardButton("10 USDT", callback_data="recharge_10")],
        [InlineKeyboardButton("20 USDT", callback_data="recharge_20")],
        [InlineKeyboardButton("50 USDT", callback_data="recharge_50")],
        [InlineKeyboardButton("💰 自定义金额", callback_data="recharge_custom")],
        [InlineKeyboardButton("🔙 主菜单", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(buttons)

# ================== 命令处理 ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)

    if user_id not in user_balances:
        user_balances[user_id] = 0.0
        save_all_data()

    try:
        admin_user = await context.bot.get_chat(ADMIN_USER_ID)
        admin_name = admin_user.full_name or admin_user.username or "管理员"
    except:
        admin_name = "管理员"

    welcome_text = get_welcome_message(admin_name)
    is_admin_user = is_admin(update.effective_user.id)

    await update.message.reply_text(welcome_text, parse_mode=None)

    shop_name = await get_shop_name(context)
    balance_text = f"{shop_name}\n\n您的余额：{user_balances[user_id]:.4f} USDT"
    reply_keyboard = get_main_reply_keyboard(is_admin_user)
    await update.message.reply_text(balance_text, reply_markup=reply_keyboard, parse_mode="Markdown")

async def refresh_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 权限不足")
        return

    global products, cards, categories, user_balances, orders, recharge_orders
    products = load_json(PRODUCTS_FILE, {})
    cards = load_json(CARD_FILE, {})
    categories = load_json(CATEGORIES_FILE, {})
    user_balances = load_json(BALANCE_FILE)
    orders = load_json(ORDER_FILE)
    recharge_orders = load_json(RECHARGE_ORDERS_FILE)

    cleaned_count = 0
    for product_key in cards:
        original_count = len(cards[product_key])
        cards[product_key] = [c for c in cards[product_key] if c.get('card', '') not in MENU_BUTTONS]
        cleaned_count += original_count - len(cards[product_key])

    if cleaned_count > 0:
        save_json(CARD_FILE, cards)

    await update.message.reply_text(
        f"✅ 数据已刷新！\n\n"
        f"📦 商品数量：{len(products)}\n"
        f"📁 分类数量：{len(categories)}\n"
        f"🧹 清理无效卡密：{cleaned_count} 条\n\n"
        f"📋 商品详情：\n"
        + "\n".join([f"• {prod['name']} → {categories.get(prod.get('category_id', ''), '无分类')} (库存:{get_product_stock(key)})" for key, prod in list(products.items())[:5]])
    )

async def fix_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 权限不足")
        return

    fixed_count = 0
    for product_key in list(cards.keys()):
        if product_key not in products:
            del cards[product_key]
            fixed_count += 1
            logger.info(f"删除无商品的卡片: {product_key}")

    save_json(CARD_FILE, cards)

    await update.message.reply_text(
        f"✅ 数据修复完成！\n\n"
        f"🧹 删除孤立卡片：{fixed_count} 条\n"
        f"📦 有效商品：{len(products)}\n"
        f"💳 有效卡片组：{len(cards)}"
    )

async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    text = update.message.text
    is_admin_user = is_admin(update.effective_user.id)

    # 分类重命名处理
    if context.user_data.get('editing_category') and is_admin_user:
        new_name = text.strip()
        old_cat_id = context.user_data.get('editing_category_old')
        
        if not new_name:
            await update.message.reply_text("❌ 分类名不能为空！")
            return
        
        if old_cat_id in categories:
            categories[old_cat_id] = new_name
            save_all_data()
            
            await update.message.reply_text(
                f"✅ 分类已重命名：\n{new_name}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📁 管理分类", callback_data="manage_categories")],
                    [InlineKeyboardButton("🔙 管理面板", callback_data="admin_panel")]
                ])
            )
        else:
            await update.message.reply_text("❌ 分类不存在！")
        
        context.user_data.pop('editing_category', None)
        context.user_data.pop('editing_category_old', None)
        return

    # 添加分类模式
    if context.user_data.get('adding_category') and is_admin_user:
        new_name = text.strip()
        
        if not new_name:
            await update.message.reply_text("❌ 分类名不能为空！")
            return
        
        cat_id = f"cat_{uuid4().hex[:8]}"
        
        if cat_id in categories:
            await update.message.reply_text("❌ 分类ID冲突，请重试")
            return
        
        categories[cat_id] = new_name
        save_all_data()
        
        await update.message.reply_text(
            f"✅ 已添加新分类：{new_name}\n\n"
            f"📁 当前共有 {len(categories)} 个分类",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📁 管理分类", callback_data="manage_categories")],
                [InlineKeyboardButton("🔙 管理面板", callback_data="admin_panel")]
            ])
        )
        
        context.user_data.pop('adding_category', None)
        return

    # 等待商品信息
    if context.user_data.get('awaiting_product_info'):
        category_id = context.user_data.get('adding_product_category')

        if not category_id:
            await update.message.reply_text("❌ 请重新点击添加商品按钮")
            context.user_data.clear()
            return

        if category_id not in categories:
            await update.message.reply_text(f"❌ 无效的分类！")
            context.user_data.clear()
            return

        parts = text.split("|")
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ 格式错误！\n\n请使用格式：`商品名称 | 价格 | 商品描述`\n\n示例：`美国老号 | 0.8 | 2018年注册带好友`",
                parse_mode="Markdown"
            )
            return

        product_name = parts[0].strip()
        try:
            product_price = float(parts[1].strip())
        except:
            await update.message.reply_text("❌ 价格必须是数字！")
            return

        product_desc = parts[2].strip() if len(parts) >= 3 else "无"

        product_key = uuid4().hex[:16]

        new_product = {
            "name": product_name,
            "price_usdt": product_price,
            "category_id": category_id,
            "description": product_desc,
            "product_type": "card",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        products[product_key] = new_product
        save_all_data()

        logger.info(f"✅ 商品已创建: {product_key} - {product_name}")

        context.user_data['awaiting_product_info'] = False
        context.user_data['awaiting_stock'] = product_key

        category_name = categories.get(category_id, "未知")
        await update.message.reply_text(
            f"✅ 商品已创建！\n\n"
            f"📦 {escape_markdown(product_name)}\n"
            f"💰 {product_price} USDT\n"
            f"📁 {escape_markdown(category_name)}\n"
            f"📝 {escape_markdown(product_desc)}\n\n"
            f"📤 请发送卡密内容（每行一个卡密），或发送「跳过」稍后添加：\n\n"
            f"例如：\nTG001-TOKEN-abc123\n\n"
            f"也可以直接发送 .txt 文件",
            parse_mode="Markdown"
        )
        return

    # 等待卡密
    if context.user_data.get('awaiting_stock'):
        product_key = context.user_data['awaiting_stock']
        prod = safe_product_get(product_key)
        
        if not prod:
            await update.message.reply_text("❌ 商品不存在，请重新操作")
            context.user_data.pop('awaiting_stock', None)
            return

        if text and text != "跳过":
            if text in MENU_BUTTONS:
                await update.message.reply_text("❌ 不能将菜单按钮添加为卡密！请重新输入正确的卡密内容。")
                return

            lines = text.split('\n')
            filtered_lines = [line for line in lines if line.strip() and line.strip() not in MENU_BUTTONS]
            added = add_cards_bulk(product_key, filtered_lines)
            current_stock = get_product_stock(product_key)

            category_id = prod.get('category_id', '')
            reply_markup = None
            if category_id:
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 返回商品分类", callback_data=f"cat_{category_id}")
                ]])

            await update.message.reply_text(
                f"✅ 已添加 {added} 个卡密\n\n"
                f"📊 当前库存：{current_stock} 个\n\n"
                f"商品已上架成功！",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                f"⚠️ 已跳过添加卡密，库存为0\n\n后续可通过商品管理添加卡密。"
            )

        context.user_data.pop('awaiting_stock', None)
        return

    # 自定义充值
    if context.user_data.get('awaiting_recharge'):
        try:
            amount = float(text)
            if amount < 1:
                await update.message.reply_text("❌ 金额不能小于 1 USDT")
                return
            order_number = f"D{datetime.now().strftime('%Y%m%d%H%M%S')}{user_id[-6:]}"
            recharge_orders[order_number] = {"user_id": user_id, "amount": amount, "status": "pending", "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            save_all_data()
            result = okpay_create_deposit(order_number, amount, user_id)
            if result.get('code') == 200:
                pay_url = result.get('data', {}).get('pay_url', '')
                await update.message.reply_text(f"💳 *充值订单*\n\n金额：{amount} USDT\n订单号：`{order_number}`\n\n[点击支付]({pay_url})\n\n支付后自动到账。", parse_mode="Markdown", disable_web_page_preview=True)
            else:
                await update.message.reply_text(f"❌ 创建失败：{result.get('msg')}")
            context.user_data.pop('awaiting_recharge', None)
        except:
            await update.message.reply_text("❌ 请输入数字金额")
        return

    # 修改价格
    if context.user_data.get('changing_price') and is_admin_user:
        product_key = context.user_data['changing_price']
        prod = safe_product_get(product_key)
        if not prod:
            await update.message.reply_text("❌ 商品不存在")
            context.user_data.pop('changing_price', None)
            return
        try:
            new_price = float(text)
            prod['price_usdt'] = new_price
            save_all_data()
            await update.message.reply_text(f"✅ 价格已修改为 {new_price} USDT")
        except:
            await update.message.reply_text("❌ 价格格式错误")
        context.user_data.pop('changing_price', None)
        return

    # 修改描述
    if context.user_data.get('changing_desc') and is_admin_user:
        product_key = context.user_data['changing_desc']
        prod = safe_product_get(product_key)
        if not prod:
            await update.message.reply_text("❌ 商品不存在")
            context.user_data.pop('changing_desc', None)
            return
        prod['description'] = text
        save_all_data()
        await update.message.reply_text(f"✅ 描述已修改")
        context.user_data.pop('changing_desc', None)
        return

    # 重命名商品
    if context.user_data.get('renaming_product') and is_admin_user:
        product_key = context.user_data['renaming_product']
        prod = safe_product_get(product_key)
        if not prod:
            await update.message.reply_text("❌ 商品不存在")
            context.user_data.pop('renaming_product', None)
            return
        new_name = text.strip()
        if new_name:
            old_name = prod['name']
            prod['name'] = new_name
            save_all_data()
            await update.message.reply_text(f"✅ 商品已重命名：\n{escape_markdown(old_name)} → {escape_markdown(new_name)}", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ 名称不能为空")
        context.user_data.pop('renaming_product', None)
        return

    # 普通按钮消息
    if text == "📦 自助购买":
        await update.message.reply_text(
            "📂 *商品分类*\n\n"
            "🛒选择你需要的商品:✅未购买过本店商品的，请先少量购买测试，以免产生纠纷！谢谢合作‼️",
            reply_markup=get_product_categories_keyboard(is_admin_user),
            parse_mode="Markdown"
        )
    elif text == "💰 我的余额":
        balance = user_balances.get(user_id, 0.0)
        await update.message.reply_text(f"💰 *我的余额*\n\n`{balance:.4f} USDT`", parse_mode="Markdown")
    elif text == "💎 充值余额":
        await update.message.reply_text(
            "💎 *充值中心*\n\n有钱人请适当充值余额目前仅对接okpay支付",
            reply_markup=get_recharge_keyboard(),
            parse_mode="Markdown"
        )
    elif text == "📋 购买记录":
        user_orders = []
        for oid, o in orders.items():
            if o.get('user_id') == user_id:
                safe_name = escape_markdown(o.get('product_name', '未知'))
                user_orders.append(f"`{oid}` - {safe_name} - {o.get('price_usdt', 0)} USDT")
        text_msg = "📋 *购买记录*\n\n" + "\n".join(user_orders[-10:]) if user_orders else "📋 暂无购买记录"
        await update.message.reply_text(text_msg, parse_mode="Markdown")
    elif text == "📞 联系客服":
        await update.message.reply_text(f"👤 *联系客服*\n\n@apl520", parse_mode="Markdown")
    elif text == "⚙️ 管理面板" and is_admin_user:
        await update.message.reply_text(
            "⚙️ *管理员面板*\n\n尊敬的管理员请进行操作当前版本v3：",
            reply_markup=get_admin_panel_keyboard(),
            parse_mode="Markdown"
        )

# ================== 按钮回调处理 ==================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = str(query.from_user.id)
    is_admin_user = is_admin(query.from_user.id)

    if data == "noop":
        return

    # ========== 主菜单 ==========
    if data == "main_menu":
        keyboard = await get_main_menu_keyboard(context, is_admin_user)
        await query.edit_message_text("🏠 *主菜单*", reply_markup=keyboard, parse_mode="Markdown")
        return

    # ========== 管理面板 ==========
    if data == "admin_panel" and is_admin_user:
        await query.edit_message_text(
            "⚙️ *管理员面板*\n\n尊敬的管理员请进行操作当前版本v3：",
            reply_markup=get_admin_panel_keyboard(),
            parse_mode="Markdown"
        )
        return

    if data == "refresh_data" and is_admin_user:
        global products, cards, categories
        products = load_json(PRODUCTS_FILE, {})
        cards = load_json(CARD_FILE, {})
        categories = load_json(CATEGORIES_FILE, {})
        await query.edit_message_text(
            f"✅ 数据已刷新！\n\n📦 商品数：{len(products)}\n📁 分类数：{len(categories)}",
            reply_markup=get_admin_panel_keyboard(),
            parse_mode="Markdown"
        )
        return

    if data == "fix_data" and is_admin_user:
        fixed = 0
        for product_key in list(cards.keys()):
            if product_key not in products:
                del cards[product_key]
                fixed += 1
        save_json(CARD_FILE, cards)
        await query.edit_message_text(
            f"✅ 修复完成！删除了 {fixed} 个孤立卡片",
            reply_markup=get_admin_panel_keyboard(),
            parse_mode="Markdown"
        )
        return

    # ========== 分类管理 ==========
    if data == "manage_categories" and is_admin_user:
        cat_buttons = []
        for cat_id, cat_name in categories.items():
            prod_count = sum(1 for prod in products.values() if prod.get('category_id') == cat_id)
            total_stock = sum(get_product_stock(key) for key, prod in products.items() if prod.get('category_id') == cat_id)
            
            if cat_id in FIXED_CATEGORIES:
                cat_buttons.append([
                    InlineKeyboardButton(f"🔒 {cat_name} ({prod_count}商品,库存:{total_stock})", callback_data="noop")
                ])
            else:
                cat_buttons.append([
                    InlineKeyboardButton(f"📁 {cat_name} ({prod_count}商品,库存:{total_stock})", callback_data=f"edit_cat_{cat_id}"),
                    InlineKeyboardButton("🗑️", callback_data=f"delete_cat_{cat_id}")
                ])
        
        cat_buttons.append([InlineKeyboardButton("➕ 添加新分类", callback_data="add_category")])
        cat_buttons.append([InlineKeyboardButton("🔙 返回管理面板", callback_data="admin_panel")])
        
        await query.edit_message_text(
            "📁 *分类管理*\n\n"
            "🔒 = 固定分类（不可删除）\n"
            f"📊 总计：{len(categories)} 个分类\n\n"
            "点击垃圾桶图标删除自定义分类",
            reply_markup=InlineKeyboardMarkup(cat_buttons),
            parse_mode="Markdown"
        )
        return

    if data == "add_category" and is_admin_user:
        context.user_data['adding_category'] = True
        await query.edit_message_text(
            "➕ *添加新分类*\n\n"
            "请发送分类名称（支持emoji）：\n\n"
            "例如：`🐆 各国豹子号`\n\n"
            "发送 /cancel 取消",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 返回分类管理", callback_data="manage_categories")]
            ]),
            parse_mode="Markdown"
        )
        return

    if data.startswith("delete_cat_") and is_admin_user:
        cat_id = data[11:]
        
        if cat_id in FIXED_CATEGORIES:
            await query.answer("⚠️ 固定分类不可删除！", show_alert=True)
            return
        
        has_products = any(prod.get('category_id') == cat_id for prod in products.values())
        
        if has_products:
            await query.answer(
                f"⚠️ 该分类下还有商品，请先删除或移动商品！",
                show_alert=True
            )
            return
        
        if cat_id in categories:
            cat_name = categories[cat_id]
            del categories[cat_id]
            save_all_data()
            await query.answer(f"✅ 已删除分类「{cat_name}」", show_alert=True)
            
            cat_buttons = []
            for cid, cname in categories.items():
                prod_count = sum(1 for prod in products.values() if prod.get('category_id') == cid)
                total_stock = sum(get_product_stock(key) for key, prod in products.items() if prod.get('category_id') == cid)
                
                if cid in FIXED_CATEGORIES:
                    cat_buttons.append([
                        InlineKeyboardButton(f"🔒 {cname} ({prod_count}商品,库存:{total_stock})", callback_data="noop")
                    ])
                else:
                    cat_buttons.append([
                        InlineKeyboardButton(f"📁 {cname} ({prod_count}商品,库存:{total_stock})", callback_data=f"edit_cat_{cid}"),
                        InlineKeyboardButton("🗑️", callback_data=f"delete_cat_{cid}")
                    ])
            
            cat_buttons.append([InlineKeyboardButton("➕ 添加新分类", callback_data="add_category")])
            cat_buttons.append([InlineKeyboardButton("🔙 返回管理面板", callback_data="admin_panel")])
            
            await query.edit_message_text(
                "📁 *分类管理*\n\n"
                "🔒 = 固定分类（不可删除）\n"
                f"📊 总计：{len(categories)} 个分类",
                reply_markup=InlineKeyboardMarkup(cat_buttons),
                parse_mode="Markdown"
            )
        return

    if data.startswith("edit_cat_") and is_admin_user:
        cat_id = data[9:]
        
        if cat_id in FIXED_CATEGORIES:
            await query.answer("⚠️ 固定分类不可编辑！", show_alert=True)
            return
        
        if cat_id not in categories:
            await query.answer("❌ 分类不存在！", show_alert=True)
            return
        
        context.user_data['editing_category'] = True
        context.user_data['editing_category_old'] = cat_id
        
        cat_name = categories.get(cat_id, "")
        await query.edit_message_text(
            f"✏️ *编辑分类*\n\n"
            f"当前名称：{cat_name}\n\n"
            f"请发送新的分类名称：",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 返回分类管理", callback_data="manage_categories")]
            ]),
            parse_mode="Markdown"
        )
        return

    # ========== 分类选择 ==========
    if data == "product_list":
        await query.edit_message_text(
            "📂 *商品分类*\n\n"
            "🛒选择你需要的商品:✅未购买过本店商品的，请先少量购买测试，以免产生纠纷！谢谢合作‼️",
            reply_markup=get_product_categories_keyboard(is_admin_user),
            parse_mode="Markdown"
        )
        return

    if data.startswith("cat_"):
        cat_id = data[4:]
        cat_name = categories.get(cat_id, "未知分类")
        await query.edit_message_text(
            f"📁 *{escape_markdown(cat_name)}*\n\n"
            f"✅未购买过本店商品的，请先少量购买测试，以免产生纠纷！谢谢合作",
            reply_markup=get_products_by_category(cat_id, is_admin_user),
            parse_mode="Markdown"
        )
        return

    # ========== 商品详情查看 ==========
    if data.startswith("view_product_"):
        product_key = data[13:]
        prod = safe_product_get(product_key)

        if not prod:
            await query.edit_message_text("❌ 商品不存在")
            return

        stock = get_product_stock(product_key)
        category_id = prod.get('category_id', '')
        cat_name = categories.get(category_id, "未知")
        safe_name = escape_markdown(prod.get('name', '未知'))
        safe_desc = escape_markdown(prod.get('description', '无'))

        if stock <= 0:
            await query.edit_message_text(
                f"❌ *{safe_name}* 已售罄！",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{category_id}")]]),
                parse_mode="Markdown"
            )
            return

        detail_text = (
            f"📦 *{safe_name}*\n\n"
            f"💰 价格：`{prod.get('price_usdt', 0)} USDT`\n"
            f"📊 库存：`{stock}` 个\n"
            f"📁 分类：{escape_markdown(cat_name)}\n"
            f"📝 商品描述：\n{safe_desc}\n\n"
            f"⚡ 点击「立即购买」将使用余额直接购买"
        )

        await query.edit_message_text(
            detail_text,
            reply_markup=get_product_detail_keyboard(product_key, category_id),
            parse_mode="Markdown"
        )
        return

    # ========== 添加商品（管理员） ==========
    if data.startswith("add_product_to_"):
        cat_id = data[len("add_product_to_"):]

        if cat_id not in categories:
            await query.edit_message_text("❌ 无效的分类！")
            return

        context.user_data['adding_product_category'] = cat_id
        context.user_data['awaiting_product_info'] = True
        await query.edit_message_text(
            "➕ *添加商品*\n\n"
            "请一次性输入以下信息，用 | 分隔：\n\n"
            "格式：`商品名称 | 价格 | 商品描述`\n\n"
            "示例：`美国老号 | 0.8 | 2018年注册带好友`\n\n"
            "发送后会自动创建商品并进入卡密添加页面。",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 取消", callback_data="product_list")]]),
            parse_mode="Markdown"
        )
        return

    # ========== 管理员管理商品 ==========
    if data.startswith("admin_manage_"):
        if not is_admin_user:
            await query.edit_message_text("⛔ 权限不足")
            return
        product_key = data[13:]
        prod = safe_product_get(product_key)
        if not prod:
            await query.edit_message_text("❌ 商品不存在")
            return

        stock = get_product_stock(product_key)
        category_id = prod.get('category_id', '')
        cat_name = categories.get(category_id, "未知")
        safe_name = escape_markdown(prod.get('name', '未知'))
        safe_desc = escape_markdown(prod.get('description', '无'))

        await query.edit_message_text(
            f"📦 *{safe_name}*\n\n"
            f"💰 价格：`{prod.get('price_usdt', 0)} USDT`\n"
            f"📝 描述：{safe_desc}\n"
            f"📊 库存：`{stock}`\n"
            f"📁 分类：{escape_markdown(cat_name)}\n\n"
            f"👇 选择操作：",
            reply_markup=get_product_action_keyboard(product_key, category_id),
            parse_mode="Markdown"
        )
        return

    if data.startswith("change_price_"):
        product_key = data[13:]
        prod = safe_product_get(product_key)
        if not prod:
            await query.edit_message_text("❌ 商品不存在")
            return
        context.user_data['changing_price'] = product_key
        await query.edit_message_text(
            f"💰 请输入 {escape_markdown(prod['name'])} 的新价格 (USDT)：",
            parse_mode="Markdown"
        )
        return

    if data.startswith("change_desc_"):
        product_key = data[12:]
        prod = safe_product_get(product_key)
        if not prod:
            await query.edit_message_text("❌ 商品不存在")
            return
        context.user_data['changing_desc'] = product_key
        await query.edit_message_text(
            f"📝 请输入 {escape_markdown(prod['name'])} 的新描述：",
            parse_mode="Markdown"
        )
        return

    if data.startswith("add_stock_"):
        product_key = data[10:]
        prod = safe_product_get(product_key)
        if not prod:
            await query.edit_message_text("❌ 商品不存在")
            return
        context.user_data['awaiting_stock'] = product_key
        await query.edit_message_text(
            f"📦 *添加卡密到 {escape_markdown(prod['name'])}*\n\n"
            f"每行一个卡密\n\n也可以直接发送 .txt 文件\n\n发送「跳过」可不添加：",
            parse_mode="Markdown"
        )
        return

    if data.startswith("view_stock_"):
        product_key = data[11:]
        prod = safe_product_get(product_key)
        if not prod:
            await query.edit_message_text("❌ 商品不存在")
            return
        product_cards = cards.get(product_key, [])
        unused = [c for c in product_cards if not c.get('used', False)]
        used = [c for c in product_cards if c.get('used', False)]
        safe_name = escape_markdown(prod.get('name', '未知'))
        text = f"📋 *{safe_name} 卡密列表*\n\n🔑 总计：{len(product_cards)}\n✅ 未使用：{len(unused)}\n❌ 已使用：{len(used)}\n\n"
        if unused:
            text += "*未使用（最近10条）：*\n"
            for c in unused[-10:]:
                text += f"`{escape_markdown(str(c['card']))}`\n"
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data=f"admin_manage_{product_key}")]]),
            parse_mode="Markdown"
        )
        return

    if data.startswith("delete_product_"):
        if not is_admin_user:
            await query.edit_message_text("⛔ 权限不足")
            return
        product_key = data[15:]
        prod = safe_product_get(product_key)
        if not prod:
            await query.edit_message_text("❌ 商品不存在")
            return
        product_name = prod.get('name', '未知')
        category_id = prod.get('category_id', '')
        del products[product_key]
        if product_key in cards:
            del cards[product_key]
        save_all_data()
        await query.edit_message_text(
            f"✅ 已删除商品：{escape_markdown(product_name)}",
            reply_markup=get_products_by_category(category_id, True),
            parse_mode="Markdown"
        )
        return

    if data.startswith("rename_product_"):
        if not is_admin_user:
            await query.edit_message_text("⛔ 权限不足")
            return
        product_key = data[16:]
        prod = safe_product_get(product_key)
        if not prod:
            await query.edit_message_text("❌ 商品不存在")
            return
        context.user_data['renaming_product'] = product_key
        await query.edit_message_text(
            f"✏️ 请输入商品的新名称：\n\n当前名称：{escape_markdown(prod.get('name', '未知'))}",
            parse_mode="Markdown"
        )
        return

    if data.startswith("back_to_category_"):
        category_id = data[17:]
        cat_name = categories.get(category_id, "未知分类")
        await query.edit_message_text(
            f"📁 *{escape_markdown(cat_name)}*\n\n"
            f"✅未购买过本店商品的，请先少量购买测试，以免产生纠纷！谢谢合作",
            reply_markup=get_products_by_category(category_id, is_admin_user),
            parse_mode="Markdown"
        )
        return

    # ========== 统计和订单 ==========
    if data == "admin_stats" and is_admin_user:
        total_users = len(user_balances)
        total_revenue = sum(o.get('price_usdt', 0) for o in orders.values())
        total_orders = len(orders)
        total_products = len(products)
        await query.edit_message_text(
            f"📊 *店铺统计*\n\n"
            f"👥 用户数：{total_users}\n"
            f"📦 订单数：{total_orders}\n"
            f"💰 营业额：{total_revenue:.2f} USDT\n"
            f"📦 商品数：{total_products}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_panel")]]),
            parse_mode="Markdown"
        )
        return

    if data == "admin_orders" and is_admin_user:
        if not orders:
            await query.edit_message_text("暂无订单")
            return
        text = "📋 *最近20条订单*\n\n"
        for oid, o in list(orders.items())[-20:]:
            safe_name = escape_markdown(o.get('product_name', 'unknown'))
            text += f"`{oid}` | {o.get('user_id', 'unknown')[-6:]} | {safe_name} | {o.get('price_usdt', 0)} USDT\n"
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_panel")]]),
            parse_mode="Markdown"
        )
        return

    if data == "admin_recharge_records" and is_admin_user:
        if not recharge_orders:
            await query.edit_message_text("暂无充值记录")
            return
        text = "💰 *充值记录*\n\n"
        for order_no, order in list(recharge_orders.items())[-20:]:
            status_emoji = "✅" if order.get("status") == "completed" else "⏳"
            text += f"{status_emoji} `{order_no}` | {order['amount']} USDT | {order['user_id'][-6:]}\n"
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_panel")]]),
            parse_mode="Markdown"
        )
        return

    if data == "admin_balance" and is_admin_user:
        result = okpay_balance()
        if result.get('code') == 200:
            balances = result.get('data', {})
            text = f"💎 *商户余额*\n\n"
            for coin, bal in balances.items():
                text += f"{coin.upper()}: `{bal}`\n"
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_panel")]])
            )
        else:
            await query.edit_message_text(f"❌ 查询失败: {result.get('msg')}")
        return

    # ========== 用户端 ==========
    if data == "contact_admin":
        await query.edit_message_text(
            f"👤 *联系客服*\n\n@apl520",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="main_menu")]]),
            parse_mode="Markdown"
        )
        return

    if data == "my_balance":
        balance = user_balances.get(user_id, 0.0)
        await query.edit_message_text(
            f"💰 *我的余额*\n\n`{balance:.4f} USDT`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 充值", callback_data="recharge_balance")],
                [InlineKeyboardButton("🔙 返回", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return

    if data == "recharge_balance":
        await query.edit_message_text(
            "💎 *充值中心*\n\n有钱人请适当充值余额目前仅对接okpay支付",
            reply_markup=get_recharge_keyboard(),
            parse_mode="Markdown"
        )
        return

    if data == "my_orders":
        user_orders = []
        for oid, o in orders.items():
            if o.get('user_id') == user_id:
                safe_name = escape_markdown(o.get('product_name', '未知'))
                user_orders.append(f"`{oid}` - {safe_name} - {o.get('price_usdt', 0)} USDT")
        text = "📋 *购买记录*\n\n" + "\n".join(user_orders[-10:]) if user_orders else "📋 暂无购买记录"
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 继续购买", callback_data="product_list")],
                [InlineKeyboardButton("🔙 返回", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return

    # ========== 用户购买 ==========
    if data.startswith("user_buy_"):
        product_key = data[9:]
        prod = safe_product_get(product_key)
        if not prod:
            await query.edit_message_text("❌ 商品不存在")
            return

        stock = get_product_stock(product_key)
        category_id = prod.get('category_id', '')
        
        if stock <= 0:
            await query.edit_message_text(
                f"❌ *{escape_markdown(prod.get('name', '未知'))}* 已售罄！",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{category_id}")]]),
                parse_mode="Markdown"
            )
            return

        price = prod.get("price_usdt", 0)
        balance = user_balances.get(user_id, 0.0)

        if balance < price:
            await query.edit_message_text(
                f"⚠️ *余额不足！*\n\n"
                f"需要：`{price} USDT`\n"
                f"当前：`{balance:.4f} USDT`\n\n"
                f"💎 请先充值",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💎 立即充值", callback_data="recharge_balance")],
                    [InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{category_id}")]
                ]),
                parse_mode="Markdown"
            )
            return

        delivery_data = get_available_card(product_key)

        if not delivery_data:
            await query.edit_message_text(
                "❌ 发货失败，请联系客服",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{category_id}")]])
            )
            return

        with balance_lock:
            user_balances[user_id] = balance - price
        save_all_data()

        order_id = create_order(user_id, product_key, prod.get('name', '未知'), price, delivery_data)

        safe_name = escape_markdown(prod.get('name', '未知'))
        safe_card = escape_markdown(delivery_data)
        
        await query.edit_message_text(
            f"✅ *购买成功！*\n\n"
            f"📦 {safe_name}\n"
            f"💰 {price} USDT\n"
            f"💎 剩余余额：`{user_balances[user_id]:.4f} USDT`\n\n"
            f"🔐 *卡密信息：*\n`{safe_card}`\n\n"
            f"📋 订单号：`{order_id}`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 继续购买", callback_data="product_list")],
                [InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{category_id}")],
                [InlineKeyboardButton("🏠 主菜单", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return

    # ========== 充值 ==========
    if data.startswith("recharge_") and data not in ["recharge_balance", "recharge_custom"]:
        amount = float(data.replace("recharge_", ""))
        order_number = f"D{datetime.now().strftime('%Y%m%d%H%M%S')}{user_id[-6:]}"
        recharge_orders[order_number] = {"user_id": user_id, "amount": amount, "status": "pending", "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        save_all_data()
        result = okpay_create_deposit(order_number, amount, user_id)
        if result.get('code') == 200:
            pay_url = result.get('data', {}).get('pay_url', '')
            await query.edit_message_text(
                f"💳 *充值订单*\n\n"
                f"💰 {amount} USDT\n"
                f"📦 订单号：`{order_number}`\n\n"
                f"[点击支付]({pay_url})\n\n"
                f"支付后自动到账。",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ 查询到账", callback_data=f"check_order_{order_number}")],
                    [InlineKeyboardButton("🔙 返回", callback_data="recharge_balance")]
                ]),
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        else:
            await query.edit_message_text(f"❌ 创建失败：{result.get('msg')}")
        return

    if data.startswith("check_order_"):
        order_number = data[12:]
        result = okpay_check_deposit(order_number)
        if result.get('code') == 200:
            resp_data = result.get('data', {})
            if resp_data.get('status') == 1:
                if confirm_recharge(order_number, resp_data.get('tx_id')):
                    await query.edit_message_text(
                        f"✅ *充值成功！*\n\n当前余额：`{user_balances.get(user_id, 0):.4f} USDT`",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 主菜单", callback_data="main_menu")]]),
                        parse_mode="Markdown"
                    )
                else:
                    await query.edit_message_text("⚠️ 处理中，请稍后")
            else:
                await query.edit_message_text(
                    f"⏳ *未支付*\n\n订单号：`{order_number}`",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 再次查询", callback_data=f"check_order_{order_number}")],
                        [InlineKeyboardButton("🔙 返回", callback_data="recharge_balance")]
                    ]),
                    parse_mode="Markdown"
                )
        else:
            await query.edit_message_text(f"❌ 查询失败")
        return

    if data == "recharge_custom":
        context.user_data['awaiting_recharge'] = True
        await query.edit_message_text(
            "✏️ *自定义充值*\n\n请输入充值金额 (USDT)，最低 1 USDT：",
            parse_mode="Markdown"
        )
        return

# ================== 文件上传处理 ==================
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ 权限不足")
        return

    document = update.message.document
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text("❌ 请上传 .txt 文件")
        return

    if not context.user_data.get('awaiting_stock'):
        await update.message.reply_text("❌ 请先点击「添加卡密」按钮")
        return

    try:
        file = await context.bot.get_file(document.file_id)
        file_content = await file.download_as_bytearray()

        content = None
        for encoding in ['utf-8-sig', 'utf-8', 'gbk', 'gb2312', 'big5', 'latin-1']:
            try:
                content = file_content.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        
        if content is None:
            await update.message.reply_text("❌ 无法识别文件编码")
            return

        lines = [line.strip() for line in content.split('\n') if line.strip()]
        filtered_lines = [line for line in lines if line not in MENU_BUTTONS]

        product_key = context.user_data['awaiting_stock']
        prod = safe_product_get(product_key)
        
        if not prod:
            await update.message.reply_text("❌ 商品不存在")
            context.user_data.pop('awaiting_stock', None)
            return

        added = add_cards_bulk(product_key, filtered_lines)
        current_stock = get_product_stock(product_key)

        category_id = prod.get('category_id', '')
        reply_markup = None
        if category_id:
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 返回商品分类", callback_data=f"cat_{category_id}")
            ]])

        await update.message.reply_text(
            f"✅ 已添加 {added} 个卡密\n\n"
            f"📊 当前库存：{current_stock} 个\n\n"
            f"商品已上架成功！",
            reply_markup=reply_markup
        )

        context.user_data.pop('awaiting_stock', None)
    except Exception as e:
        logger.error(f"文件处理失败: {e}")
        await update.message.reply_text(f"❌ 读取失败：{e}")

async def cancel_operation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("操作已取消。")
    context.user_data.clear()
    return ConversationHandler.END

# ================== 主程序 ==================
async def post_init(application: Application) -> None:
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(check_pending_recharges, interval=30, first=10)
    else:
        logger.warning("⚠️ JobQueue 未安装，自动到账检查不可用。请安装: pip install python-telegram-bot[job-queue]")

def main() -> None:
    application = Application.builder().token(TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel_operation))
    application.add_handler(CommandHandler("refresh", refresh_data))
    application.add_handler(CommandHandler("fix", fix_data))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_all_messages))

    print("🤖 自助购买机器人已启动！")
    print(f"📂 固定分类：{list(FIXED_CATEGORIES.values())}")
    print(f"📦 商品数量：{len(products)}")
    print(f"👥 用户数量：{len(user_balances)}")
    print(f"📦 订单数量：{len(orders)}")
    print("💎 OkayPay API 已集成")
    print("🔒 已启用并发安全锁")
    print("✨ Markdown 转义已启用")
    print("📁 分类管理已完善")
    print("🎉 每次 /start 都会发送完整欢迎消息")

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()