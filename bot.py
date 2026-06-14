import json
import os
import logging
import asyncio
import hashlib
import urllib.parse
import re
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
TOKEN = "8768735103:AAFcK8UiWXUT08MbPH11LELRXl_zaGwgS1E"
ADMIN_USER_ID = 7002638062
WELCOME_CHAT_IDS = [ADMIN_USER_ID]

# ================== OkayPay API 配置 ==================
API_URL = 'https://api.okaypay.me/shop/'
shop_id = "34543"
shop_token = "8fkGUXg5BszGHK1MPb3SFhWpYLt2Jwa"
NAME = "商品购买"
bot_username = "jklgf564bot"

# 数据文件路径
BALANCE_FILE = "user_balances.json"
ORDER_FILE = "orders.json"
PRODUCTS_FILE = "products.json"
COUNTRIES_FILE = "countries.json"
CARD_FILE = "cards.json"
CATEGORIES_FILE = "categories.json"
SENT_WELCOME_FILE = "sent_welcome.json"
RECHARGE_ORDERS_FILE = "recharge_orders.json"

# ================== 固定分类（标准格式） ==================
FIXED_CATEGORIES = ["🐆 各国豹子号", "🔄 各国换绑注册", "🎯 各国劫持账号", "📞 各国双向账号"]

# 菜单按钮列表（防止误保存）
MENU_BUTTONS = [
    "📦 自助购买",
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

# ================== 数据持久化 ==================
def load_json(file_path: str, default: Any = None) -> Any:
    if default is None:
        default = {} if file_path.endswith(".json") else []
    if not os.path.exists(file_path):
        return default
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default

def save_json(file_path: str, data: Any) -> None:
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# 初始化数据
user_balances: Dict[str, float] = load_json(BALANCE_FILE)
orders: Dict[str, Dict] = load_json(ORDER_FILE)
products: Dict[str, Dict] = load_json(PRODUCTS_FILE, {})
countries: Dict[str, Dict] = load_json(COUNTRIES_FILE, {})
cards: Dict[str, List[Dict]] = load_json(CARD_FILE, {})
categories: List[str] = load_json(CATEGORIES_FILE, [])
sent_welcome: Dict[str, bool] = load_json(SENT_WELCOME_FILE, {})
recharge_orders: Dict[str, Dict] = load_json(RECHARGE_ORDERS_FILE, {})

# 确保固定分类存在
for cat in FIXED_CATEGORIES:
    if cat not in categories:
        categories.append(cat)
        save_json(CATEGORIES_FILE, categories)

if products is None:
    products = {}
    save_json(PRODUCTS_FILE, products)
if cards is None:
    cards = {}
    save_json(CARD_FILE, cards)
if countries is None:
    countries = {}
    save_json(COUNTRIES_FILE, countries)

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
    if product_key not in cards:
        return None
    for i, card_info in enumerate(cards[product_key]):
        if not card_info.get("used", False):
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
        # ✅ 过滤菜单按钮
        if card and card not in MENU_BUTTONS:
            cards[product_key].append({"card": card, "used": False})
            added += 1
    save_json(CARD_FILE, cards)
    return added

def get_product_stock(product_key: str) -> int:
    """获取商品库存 - 修复：不会自动创建空库存"""
    # ✅ 不自动创建空库存
    if product_key not in cards:
        return 0
    return len([c for c in cards[product_key] if not c.get('used', False)])

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
        return f"🎫 {admin_name}の自助卖号"
    except:
        return "🎫 自助卖号机器人"

# ================== 欢迎消息 ==================
def get_welcome_message(admin_name: str) -> str:
    return (
        f"🌈欢迎光临{admin_name}自助卖号机器人 \n\n"
        "✅TG账号自助购买 \n\n"
        "1、请先少量购买测试，合适可继续购买\n\n"
        "2、购买后第一时间检测是否死号，如帐号有问题请十分钟内联系我处理，包售后，超时不售后\n\n"
        "3、群发群、拉人还是私信都有技巧，不能盲目，可以进群交流\n"
        "——————————————\n\n"
        "🛰️【频道】 https://t.me/ltnb66678\n"
        "👥【群组】 https://t.me/huhbjise\n"
        "☎️【客服】 @nbbv354\n"
        "🛠️【工具】 @NBTG1BOT\n"
        "🌐【零售】 https://buy.wlqfk.net/shop/41WFDSM2\n\n"
        "⚙ /start   ⬅点击命令打开底部菜单\n\n"
        "机器人支持USDT 人民币充值 不接受使用后售后"
    )

async def send_startup_welcome(application: Application) -> None:
    """启动时发送欢迎消息给管理员（已禁用，避免重复）"""
    pass  # 已禁用，因为每次 /start 都会发送欢迎消息

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
    """动态分类键盘 - 使用 categories"""
    global categories
    categories = load_json(CATEGORIES_FILE, [])
    
    buttons = []
    icons = {
        "🐆 各国豹子号": "🐆",
        "🔄 各国换绑注册": "🔄",
        "🎯 各国劫持账号": "🎯",
        "📞 各国双向账号": "📞"
    }
    
    for cat in categories:
        total_stock = 0
        for key, prod in products.items():
            # ✅ 宽松匹配分类
            if prod.get('category', '').strip() == cat.strip():
                total_stock += get_product_stock(key)
        icon = icons.get(cat, "📁")
        stock_text = f" (库存:{total_stock})" if total_stock > 0 else ""
        
        if is_admin:
            buttons.append([
                InlineKeyboardButton(f"{icon} {cat}{stock_text}", callback_data=f"cat_{cat}"),
                InlineKeyboardButton("➕", callback_data=f"add_product_to_{cat}")
            ])
        else:
            buttons.append([InlineKeyboardButton(f"{icon} {cat}{stock_text}", callback_data=f"cat_{cat}")])
    
    buttons.append([InlineKeyboardButton("🏠 主菜单", callback_data="main_menu")])
    return InlineKeyboardMarkup(buttons)

def get_products_by_category(category: str, is_admin: bool = False) -> InlineKeyboardMarkup:
    """分类下的商品列表 - 宽松匹配"""
    buttons = []
    
    # ✅ 标准化分类名称
    category_clean = category.strip()
    
    for key, prod in products.items():
        prod_category = prod.get('category', '').strip()
        # ✅ 宽松匹配
        if prod_category == category_clean:
            stock = get_product_stock(key)
            
            if is_admin:
                buttons.append([InlineKeyboardButton(
                    f"⚙️ {prod['name']} - {prod['price_usdt']} USDT (库存:{stock})", 
                    callback_data=f"admin_manage_{key}"
                )])
            else:
                if stock > 0:
                    buttons.append([InlineKeyboardButton(
                        f"📦 {prod['name']} - {prod['price_usdt']} USDT", 
                        callback_data=f"view_product_{key}"
                    )])
                else:
                    buttons.append([InlineKeyboardButton(
                        f"❌ {prod['name']} - 已售罄", 
                        callback_data="noop"
                    )])
    
    if not buttons:
        if is_admin:
            buttons.append([InlineKeyboardButton("➕ 添加商品到此分类", callback_data=f"add_product_to_{category}")])
        else:
            buttons.append([InlineKeyboardButton("📁 暂无商品", callback_data="noop")])
    
    buttons.append([InlineKeyboardButton("🔙 返回分类列表", callback_data="product_list")])
    return InlineKeyboardMarkup(buttons)

def get_product_detail_keyboard(product_key: str, category: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("💎 立即购买", callback_data=f"user_buy_{product_key}")],
        [InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{category}")],
        [InlineKeyboardButton("🏠 主菜单", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(buttons)

def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📊 查看统计", callback_data="admin_stats")],
        [InlineKeyboardButton("📋 所有订单", callback_data="admin_orders")],
        [InlineKeyboardButton("💰 充值记录", callback_data="admin_recharge_records")],
        [InlineKeyboardButton("💎 商户余额", callback_data="admin_balance")],
        [InlineKeyboardButton("🔄 刷新数据", callback_data="refresh_data")],
        [InlineKeyboardButton("🔧 修复数据", callback_data="fix_data")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(buttons)

def get_product_action_keyboard(product_key: str, category: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("💰 修改价格", callback_data=f"change_price_{product_key}")],
        [InlineKeyboardButton("📝 修改描述", callback_data=f"change_desc_{product_key}")],
        [InlineKeyboardButton("📦 添加卡密", callback_data=f"add_stock_{product_key}")],
        [InlineKeyboardButton("📋 查看卡密", callback_data=f"view_stock_{product_key}")],
        [InlineKeyboardButton("✏️ 重命名", callback_data=f"rename_product_{product_key}")],
        [InlineKeyboardButton("🗑️ 删除商品", callback_data=f"delete_product_{product_key}")],
        [InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{category}")]
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
    
    # 初始化用户余额（如果不存在）
    if user_id not in user_balances:
        user_balances[user_id] = 0.0
        save_all_data()
    
    # 获取管理员名称
    try:
        admin_user = await context.bot.get_chat(ADMIN_USER_ID)
        admin_name = admin_user.full_name or admin_user.username or "管理员"
    except:
        admin_name = "管理员"
    
    # 获取欢迎消息
    welcome_text = get_welcome_message(admin_name)
    
    # 判断是否为管理员
    is_admin_user = is_admin(update.effective_user.id)
    
    # 发送欢迎消息（不使用 Markdown 解析，避免格式错误）
    await update.message.reply_text(welcome_text, parse_mode=None)
    
    # 单独发送带余额的主菜单
    shop_name = await get_shop_name(context)
    balance_text = f"{shop_name}\n\n您的余额：{user_balances[user_id]:.4f} USDT"
    reply_keyboard = get_main_reply_keyboard(is_admin_user)
    await update.message.reply_text(balance_text, reply_markup=reply_keyboard, parse_mode="Markdown")

async def refresh_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """手动刷新数据命令"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 权限不足")
        return
    
    global products, cards, categories, user_balances, orders, recharge_orders
    products = load_json(PRODUCTS_FILE, {})
    cards = load_json(CARD_FILE, {})
    categories = load_json(CATEGORIES_FILE, [])
    user_balances = load_json(BALANCE_FILE)
    orders = load_json(ORDER_FILE)
    recharge_orders = load_json(RECHARGE_ORDERS_FILE)
    
    # 清理卡片中的菜单按钮
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
        + "\n".join([f"• {prod['name']} → {prod.get('category', '无分类')} (库存:{get_product_stock(key)})" for key, prod in list(products.items())[:5]])
    )

async def fix_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """修复数据 - 确保商品key一致"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 权限不足")
        return
    
    fixed_count = 0
    # 修复：确保 products 和 cards 的 key 一致
    for product_key in list(cards.keys()):
        if product_key not in products:
            # 如果卡片存在但商品不存在，删除卡片
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
    
    # 检查等待状态
    if context.user_data.get('awaiting_product_info'):
        category = context.user_data.get('adding_product_category')
        
        if not category:
            await update.message.reply_text("❌ 请重新点击添加商品按钮")
            context.user_data.clear()
            return
        
        # 验证分类是否有效
        if category not in categories:
            await update.message.reply_text(f"❌ 无效的分类！请从以下分类中选择：\n" + "\n".join(categories))
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
        
        # ✅ 使用 UUID 生成唯一商品 key（不会冲突）
        product_key = uuid4().hex[:16]
        
        new_product = {
            "name": product_name,
            "price_usdt": product_price,
            "category": category,  # 保存原始分类名
            "description": product_desc,
            "product_type": "card",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        products[product_key] = new_product
        # 注意：不自动创建 cards[product_key]，等添加卡密时再创建
        save_all_data()
        
        logger.info(f"✅ 商品已创建: {product_key}")
        logger.info(f"   名称: {product_name}")
        logger.info(f"   分类: {category}")
        logger.info(f"   价格: {product_price}")
        
        context.user_data['awaiting_product_info'] = False
        context.user_data['awaiting_stock'] = product_key
        
        await update.message.reply_text(
            f"✅ 商品已创建！\n\n"
            f"📦 {product_name}\n"
            f"💰 {product_price} USDT\n"
            f"📁 {category}\n"
            f"📝 {product_desc}\n\n"
            f"📤 请发送卡密内容（每行一个卡密），或发送「跳过」稍后添加：\n\n"
            f"例如：\nTG001-TOKEN-abc123\n\n"
            f"也可以直接发送 .txt 文件"
        )
        return
    
    if context.user_data.get('awaiting_stock'):
        product_key = context.user_data['awaiting_stock']
        
        if text and text != "跳过":
            # ✅ 过滤菜单按钮
            if text in MENU_BUTTONS:
                await update.message.reply_text("❌ 不能将菜单按钮添加为卡密！请重新输入正确的卡密内容。")
                return
            
            lines = text.split('\n')
            # ✅ 再次过滤每一行
            filtered_lines = [line for line in lines if line.strip() and line.strip() not in MENU_BUTTONS]
            added = add_cards_bulk(product_key, filtered_lines)
            current_stock = get_product_stock(product_key)
            
            category = products.get(product_key, {}).get('category', '')
            reply_markup = None
            if category:
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 返回商品分类", callback_data=f"cat_{category}")
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
    
    # 管理员修改操作
    if context.user_data.get('changing_price') and is_admin_user:
        product_key = context.user_data['changing_price']
        try:
            new_price = float(text)
            products[product_key]['price_usdt'] = new_price
            save_all_data()
            await update.message.reply_text(f"✅ 价格已修改为 {new_price} USDT")
        except:
            await update.message.reply_text("❌ 价格格式错误")
        context.user_data.pop('changing_price', None)
        return
    
    if context.user_data.get('changing_desc') and is_admin_user:
        product_key = context.user_data['changing_desc']
        products[product_key]['description'] = text
        save_all_data()
        await update.message.reply_text(f"✅ 描述已修改")
        context.user_data.pop('changing_desc', None)
        return
    
    if context.user_data.get('renaming_product') and is_admin_user:
        product_key = context.user_data['renaming_product']
        new_name = text.strip()
        if new_name:
            old_name = products[product_key]['name']
            products[product_key]['name'] = new_name
            save_all_data()
            await update.message.reply_text(f"✅ 商品已重命名：\n{old_name} → {new_name}")
        else:
            await update.message.reply_text("❌ 名称不能为空")
        context.user_data.pop('renaming_product', None)
        return
    
    # 普通按钮消息
    if text == "📦 自助购买":
        await update.message.reply_text("📂 *商品分类*\n\n🛒选择你需要的商品:✅未购买过本店商品的，请先少量购买测试，以免产生纠纷！谢谢合作‼️", reply_markup=get_product_categories_keyboard(is_admin_user), parse_mode="Markdown")
    elif text == "💰 我的余额":
        balance = user_balances.get(user_id, 0.0)
        await update.message.reply_text(f"💰 *我的余额*\n\n`{balance:.4f} USDT`", parse_mode="Markdown")
    elif text == "💎 充值余额":
        await update.message.reply_text("💎 *充值中心*\n\n有钱人请适当充值余额目前仅对接okpay支付", reply_markup=get_recharge_keyboard(), parse_mode="Markdown")
    elif text == "📋 购买记录":
        user_orders = []
        for oid, o in orders.items():
            if o.get('user_id') == user_id:
                user_orders.append(f"`{oid}` - {o['product_name']} - {o['price_usdt']} USDT")
        text_msg = "📋 *购买记录*\n\n" + "\n".join(user_orders[-10:]) if user_orders else "📋 暂无购买记录"
        await update.message.reply_text(text_msg, parse_mode="Markdown")
    elif text == "📞 联系客服":
        await update.message.reply_text(f"👤 *联系客服*\n\n@nbbv354", parse_mode="Markdown")
    elif text == "⚙️ 管理面板" and is_admin_user:
        await update.message.reply_text("⚙️ *管理员面板*\n\n选择操作：", reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown")

# ================== 按钮回调处理 ==================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = str(query.from_user.id)
    is_admin_user = is_admin(query.from_user.id)

    if data == "noop":
        return

    if data == "main_menu":
        keyboard = await get_main_menu_keyboard(context, is_admin_user)
        await query.edit_message_text("🏠 *主菜单*", reply_markup=keyboard, parse_mode="Markdown")
        return

    if data == "admin_panel" and is_admin_user:
        await query.edit_message_text("⚙️ *管理员面板*\n\n选择操作：", reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown")
        return
    
    if data == "refresh_data" and is_admin_user:
        global products, cards, categories
        products = load_json(PRODUCTS_FILE, {})
        cards = load_json(CARD_FILE, {})
        categories = load_json(CATEGORIES_FILE, [])
        await query.edit_message_text(
            f"✅ 数据已刷新！\n\n📦 商品数：{len(products)}\n📁 分类数：{len(categories)}",
            reply_markup=get_admin_panel_keyboard(),
            parse_mode="Markdown"
        )
        return
    
    if data == "fix_data" and is_admin_user:
        # 修复数据
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

    # ========== 分类选择 ==========
    if data == "product_list":
        await query.edit_message_text("📂 *商品分类*\n\n🛒选择你需要的商品:✅未购买过本店商品的，请先少量购买测试，以免产生纠纷！谢谢合作‼️", reply_markup=get_product_categories_keyboard(is_admin_user), parse_mode="Markdown")
        return
    
    if data.startswith("cat_"):
        category = data[4:]
        await query.edit_message_text(
            f"📁 *{category}*\n\n✅未购买过本店商品的，请先少量购买测试，以免产生纠纷！谢谢合作",
            reply_markup=get_products_by_category(category, is_admin_user),
            parse_mode="Markdown"
        )
        return
    
    # ========== 商品详情查看 ==========
    if data.startswith("view_product_"):
        product_key = data[13:]
        prod = products.get(product_key)
        
        if not prod:
            await query.edit_message_text("❌ 商品不存在")
            return
        
        stock = get_product_stock(product_key)
        category = prod.get('category', '')
        
        if stock <= 0:
            await query.edit_message_text(
                f"❌ *{prod['name']}* 已售罄！",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{category}")]]),
                parse_mode="Markdown"
            )
            return
        
        detail_text = (
            f"📦 *{prod['name']}*\n\n"
            f"💰 价格：`{prod['price_usdt']} USDT`\n"
            f"📊 库存：`{stock}` 个\n"
            f"📝 商品描述：\n{prod.get('description', '无')}\n\n"
            f"⚡ 点击「立即购买」将使用余额直接购买"
        )
        
        await query.edit_message_text(
            detail_text,
            reply_markup=get_product_detail_keyboard(product_key, category),
            parse_mode="Markdown"
        )
        return
    
    # ========== 添加商品（管理员） ==========
    if data.startswith("add_product_to_"):
        # ✅ 修复：使用 len 而不是硬编码
        category = data[len("add_product_to_"):]
        
        if category not in categories:
            await query.edit_message_text("❌ 无效的分类！")
            return
        
        context.user_data['adding_product_category'] = category
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
        prod = products.get(product_key)
        if not prod:
            await query.edit_message_text("❌ 商品不存在")
            return
        
        stock = get_product_stock(product_key)
        
        await query.edit_message_text(
            f"📦 *{prod['name']}*\n\n"
            f"💰 价格：`{prod['price_usdt']} USDT`\n"
            f"📝 描述：{prod.get('description', '无')}\n"
            f"📊 库存：`{stock}`\n"
            f"📁 分类：{prod.get('category')}\n\n"
            f"👇 选择操作：",
            reply_markup=get_product_action_keyboard(product_key, prod.get('category')),
            parse_mode="Markdown"
        )
        return
    
    if data.startswith("change_price_"):
        product_key = data[13:]
        context.user_data['changing_price'] = product_key
        await query.edit_message_text(f"💰 请输入 {products[product_key]['name']} 的新价格 (USDT)：", parse_mode="Markdown")
        return
    
    if data.startswith("change_desc_"):
        product_key = data[12:]
        context.user_data['changing_desc'] = product_key
        await query.edit_message_text(f"📝 请输入 {products[product_key]['name']} 的新描述：", parse_mode="Markdown")
        return
    
    if data.startswith("add_stock_"):
        product_key = data[10:]
        prod = products[product_key]
        context.user_data['awaiting_stock'] = product_key
        await query.edit_message_text(
            f"📦 *添加卡密到 {prod['name']}*\n\n"
            f"每行一个卡密\n\n也可以直接发送 .txt 文件\n\n发送「跳过」可不添加：",
            parse_mode="Markdown"
        )
        return
    
    if data.startswith("view_stock_"):
        product_key = data[11:]
        prod = products[product_key]
        product_cards = cards.get(product_key, [])
        unused = [c for c in product_cards if not c.get('used', False)]
        used = [c for c in product_cards if c.get('used', False)]
        text = f"📋 *{prod['name']} 卡密列表*\n\n🔑 总计：{len(product_cards)}\n✅ 未使用：{len(unused)}\n❌ 已使用：{len(used)}\n\n"
        if unused:
            text += "*未使用（最近10条）：*\n"
            for c in unused[-10:]:
                text += f"`{c['card']}`\n"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data=f"admin_manage_{product_key}")]]), parse_mode="Markdown")
        return
    
    if data.startswith("delete_product_"):
        if not is_admin_user:
            await query.edit_message_text("⛔ 权限不足")
            return
        product_key = data[15:]
        product_name = products[product_key]['name']
        category = products[product_key]['category']
        del products[product_key]
        if product_key in cards:
            del cards[product_key]
        save_all_data()
        await query.edit_message_text(f"✅ 已删除商品：{product_name}", reply_markup=get_products_by_category(category, True), parse_mode="Markdown")
        return
    
    if data.startswith("rename_product_"):
        if not is_admin_user:
            await query.edit_message_text("⛔ 权限不足")
            return
        product_key = data[16:]
        context.user_data['renaming_product'] = product_key
        await query.edit_message_text(
            f"✏️ 请输入商品的新名称：\n\n当前名称：{products[product_key]['name']}",
            parse_mode="Markdown"
        )
        return
    
    if data.startswith("back_to_category_"):
        category = data[17:]
        await query.edit_message_text(
            f"📁 *{category}*\n\n✅未购买过本店商品的，请先少量购买测试，以免产生纠纷！谢谢合作",
            reply_markup=get_products_by_category(category, is_admin_user),
            parse_mode="Markdown"
        )
        return

    # ========== 统计和订单（保持原有代码） ==========
    if data == "admin_stats" and is_admin_user:
        total_users = len(user_balances)
        total_revenue = sum(o.get('price_usdt', 0) for o in orders.values())
        total_orders = len(orders)
        total_products = len(products)
        await query.edit_message_text(
            f"📊 *店铺统计*\n\n👥 用户数：{total_users}\n📦 订单数：{total_orders}\n💰 营业额：{total_revenue:.2f} USDT\n📦 商品数：{total_products}",
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
            text += f"`{oid}` | {o.get('user_id', 'unknown')[-6:]} | {o['product_name']} | {o['price_usdt']} USDT\n"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_panel")]]), parse_mode="Markdown")
        return
    
    if data == "admin_recharge_records" and is_admin_user:
        if not recharge_orders:
            await query.edit_message_text("暂无充值记录")
            return
        text = "💰 *充值记录*\n\n"
        for order_no, order in list(recharge_orders.items())[-20:]:
            status_emoji = "✅" if order.get("status") == "completed" else "⏳"
            text += f"{status_emoji} `{order_no}` | {order['amount']} USDT | {order['user_id'][-6:]}\n"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_panel")]]), parse_mode="Markdown")
        return
    
    if data == "admin_balance" and is_admin_user:
        result = okpay_balance()
        if result.get('code') == 200:
            balances = result.get('data', {})
            text = f"💎 *商户余额*\n\n"
            for coin, bal in balances.items():
                text += f"{coin.upper()}: `{bal}`\n"
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="admin_panel")]]))
        else:
            await query.edit_message_text(f"❌ 查询失败: {result.get('msg')}")
        return

    # ========== 用户端 ==========
    if data == "contact_admin":
        await query.edit_message_text(f"👤 *联系客服*\n\n@nbbv354", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="main_menu")]]), parse_mode="Markdown")
        return
    
    if data == "my_balance":
        balance = user_balances.get(user_id, 0.0)
        await query.edit_message_text(f"💰 *我的余额*\n\n`{balance:.4f} USDT`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 充值", callback_data="recharge_balance")], [InlineKeyboardButton("🔙 返回", callback_data="main_menu")]]), parse_mode="Markdown")
        return
    
    if data == "recharge_balance":
        await query.edit_message_text("💎 *充值中心*\n\n有钱人请适当充值余额目前仅对接okpay支付", reply_markup=get_recharge_keyboard(), parse_mode="Markdown")
        return
    
    if data == "my_orders":
        user_orders = []
        for oid, o in orders.items():
            if o.get('user_id') == user_id:
                user_orders.append(f"`{oid}` - {o['product_name']} - {o['price_usdt']} USDT")
        text = "📋 *购买记录*\n\n" + "\n".join(user_orders[-10:]) if user_orders else "📋 暂无购买记录"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 继续购买", callback_data="product_list")], [InlineKeyboardButton("🔙 返回", callback_data="main_menu")]]), parse_mode="Markdown")
        return

    # ========== 用户购买 ==========
    if data.startswith("user_buy_"):
        product_key = data[9:]
        prod = products.get(product_key)
        if not prod:
            await query.edit_message_text("❌ 商品不存在")
            return
        
        stock = get_product_stock(product_key)
        if stock <= 0:
            await query.edit_message_text(
                f"❌ *{prod['name']}* 已售罄！",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{prod.get('category', '')}")]]),
                parse_mode="Markdown"
            )
            return
        
        price = prod["price_usdt"]
        balance = user_balances.get(user_id, 0.0)
        
        if balance < price:
            await query.edit_message_text(
                f"⚠️ *余额不足！*\n\n"
                f"需要：`{price} USDT`\n"
                f"当前：`{balance:.4f} USDT`\n\n"
                f"💎 请先充值",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💎 立即充值", callback_data="recharge_balance")],
                    [InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{prod.get('category', '')}")]
                ]),
                parse_mode="Markdown"
            )
            return
        
        delivery_data = get_available_card(product_key)
        
        if not delivery_data:
            await query.edit_message_text(
                "❌ 发货失败，请联系客服",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{prod.get('category', '')}")]])
            )
            return
        
        user_balances[user_id] = balance - price
        save_all_data()
        
        order_id = create_order(user_id, product_key, prod['name'], price, delivery_data)
        
        await query.edit_message_text(
            f"✅ *购买成功！*\n\n"
            f"📦 {prod['name']}\n"
            f"💰 {price} USDT\n"
            f"💎 剩余余额：`{user_balances[user_id]:.4f} USDT`\n\n"
            f"🔐 *卡密信息：*\n`{delivery_data}`\n\n"
            f"📋 订单号：`{order_id}`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 继续购买", callback_data="product_list")],
                [InlineKeyboardButton("🔙 返回商品列表", callback_data=f"back_to_category_{prod.get('category', '')}")],
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
                f"💳 *充值订单*\n\n💰 {amount} USDT\n📦 订单号：`{order_number}`\n\n[点击支付]({pay_url})\n\n支付后自动到账。",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ 查询到账", callback_data=f"check_order_{order_number}")], [InlineKeyboardButton("🔙 返回", callback_data="recharge_balance")]]),
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
                    await query.edit_message_text(f"✅ *充值成功！*\n\n当前余额：`{user_balances.get(user_id, 0):.4f} USDT`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 主菜单", callback_data="main_menu")]]), parse_mode="Markdown")
                else:
                    await query.edit_message_text("⚠️ 处理中，请稍后")
            else:
                await query.edit_message_text(f"⏳ *未支付*\n\n订单号：`{order_number}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 再次查询", callback_data=f"check_order_{order_number}")], [InlineKeyboardButton("🔙 返回", callback_data="recharge_balance")]]), parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ 查询失败")
        return
    
    if data == "recharge_custom":
        context.user_data['awaiting_recharge'] = True
        await query.edit_message_text("✏️ *自定义充值*\n\n请输入充值金额 (USDT)，最低 1 USDT：", parse_mode="Markdown")
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
        
        # ✅ 支持多种编码
        try:
            content = file_content.decode('utf-8')
        except UnicodeDecodeError:
            try:
                content = file_content.decode('gbk')
            except UnicodeDecodeError:
                content = file_content.decode('utf-8', errors='ignore')
        
        lines = [line.strip() for line in content.split('\n') if line.strip()]
        
        # ✅ 过滤菜单按钮
        filtered_lines = [line for line in lines if line not in MENU_BUTTONS]
        
        product_key = context.user_data['awaiting_stock']
        
        added = add_cards_bulk(product_key, filtered_lines)
        current_stock = get_product_stock(product_key)
        
        category = products.get(product_key, {}).get('category', '')
        reply_markup = None
        if category:
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 返回商品分类", callback_data=f"cat_{category}")
            ]])
        
        await update.message.reply_text(
            f"✅ 已添加 {added} 个卡密\n\n"
            f"📊 当前库存：{current_stock} 个\n\n"
            f"商品已上架成功！",
            reply_markup=reply_markup
        )
        
        context.user_data.pop('awaiting_stock', None)
    except Exception as e:
        await update.message.reply_text(f"❌ 读取失败：{e}")

async def cancel_operation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("操作已取消。")
    context.user_data.clear()
    return ConversationHandler.END

# ================== 主程序 ==================
async def post_init(application: Application) -> None:
    # 注释掉自动发送欢迎消息，避免重复（每次/start都会发送）
    # await send_startup_welcome(application)
    
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(check_pending_recharges, interval=30, first=10)

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
    print(f"📂 固定分类：{FIXED_CATEGORIES}")
    print(f"📦 商品数量：{len(products)}")
    print(f"👥 用户数量：{len(user_balances)}")
    print(f"📦 订单数量：{len(orders)}")
    print("💎 OkayPay API 已集成")
    print("✨ 已启用商品详情页功能")
    print("🎉 每次 /start 都会发送完整欢迎消息")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()