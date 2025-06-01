import os
import time
import json
import logging
import asyncio
import aiohttp
import threading
from typing import Dict, List, Optional, Tuple, Union
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, field
from enum import Enum
import sqlite3
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ContextTypes
)

# تحسين نظام السجلات
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('trading_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ——————————— التعديدات والثوابت ———————————
class OrderType(Enum):
    BUY = "buy"
    SELL = "sell"

class AlertType(Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    PRICE_ALERT = "price_alert"

class TradingStatus(Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    CANCELLED = "cancelled"

# ——————————— إعدادات البوت المحسنة ———————————
@dataclass
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
    PUBLIC_KEY: str = os.getenv("PUBLIC_KEY", "")
    
    # إعدادات قاعدة البيانات
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "trading_bot.db")
    
    # Solana RPC Endpoints مع Load Balancing
    SOLANA_RPC_ENDPOINTS: List[str] = field(default_factory=lambda: [
        "https://api.mainnet-beta.solana.com",
        "https://solana-api.projectserum.com", 
        "https://rpc.ankr.com/solana",
        "https://solana.blockdaemon.com",
        "https://api.mainnet-beta.solana.com"
    ])
    
    # API Endpoints
    COINGECKO_API: str = "https://api.coingecko.com/api/v3/simple/price"
    DEXSCREENER_API: str = "https://api.dexscreener.com/latest/dex/pairs/solana"
    JUPITER_QUOTE_API: str = "https://quote-api.jup.ag/v6/quote"
    
    # إعدادات التداول المحسنة
    MIN_SOL_AMOUNT: float = 0.01
    MAX_SOL_AMOUNT: float = 50.0
    DEFAULT_SLIPPAGE: float = 0.5  # 0.5%
    DEFAULT_BUY_AMOUNT_SOL: float = 0.1  # إضافة قيمة افتراضية
    
    # إعدادات المراقبة
    PRICE_MONITOR_INTERVAL: int = 5  # ثواني
    MAX_RETRY_ATTEMPTS: int = 3
    REQUEST_TIMEOUT: int = 15
    
    # إعدادات الأمان
    MAX_DAILY_TRADES: int = 100
    COOLDOWN_BETWEEN_TRADES: int = 1  # ثانية
    
    # إعدادات الإشعارات
    ENABLE_PRICE_ALERTS: bool = True
    ENABLE_PROFIT_NOTIFICATIONS: bool = True
    
    def validate(self) -> bool:
        """التحقق من صحة الإعدادات"""
        required_fields = ['BOT_TOKEN', 'ADMIN_ID', 'PUBLIC_KEY']
        for field in required_fields:
            if not getattr(self, field):
                logger.error(f"Missing required config: {field}")
                return False
        return True

# ——————————— نماذج البيانات المحسنة ———————————
@dataclass
class Position:
    token_address: str
    token_name: str
    token_symbol: str
    buy_amount_sol: float
    buy_price_usd: float
    tokens_amount: float
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    trailing_stop: Optional[float] = None
    status: TradingStatus = TradingStatus.ACTIVE
    created_at: datetime = None
    closed_at: Optional[datetime] = None
    pnl_usd: float = 0.0
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
    
    def to_dict(self) -> dict:
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat()
        data['closed_at'] = self.closed_at.isoformat() if self.closed_at else None
        data['status'] = self.status.value
        return data
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Position':
        data['created_at'] = datetime.fromisoformat(data['created_at'])
        data['closed_at'] = datetime.fromisoformat(data['closed_at']) if data['closed_at'] else None
        data['status'] = TradingStatus(data['status'])
        return cls(**data)

@dataclass
class TokenInfo:
    token_name: str
    token_symbol: str
    price_usd: float
    price_sol: float
    liquidity_usd: float
    market_cap_usd: float
    fdv_usd: float = 0.0
    volume_24h: float = 0.0
    price_change_24h: float = 0.0
    holders: int = 0
    created_at: datetime = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()

@dataclass
class TradingStats:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    win_rate: float = 0.0
    
    def update_stats(self, pnl: float):
        self.total_trades += 1
        self.total_pnl += pnl
        
        if pnl > 0:
            self.winning_trades += 1
            if pnl > self.best_trade:
                self.best_trade = pnl
        else:
            self.losing_trades += 1
            if pnl < self.worst_trade:
                self.worst_trade = pnl
        
        self.win_rate = (self.winning_trades / self.total_trades) * 100 if self.total_trades > 0 else 0

# ——————————— مدير قاعدة البيانات ———————————
class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_database()
    
    def _init_database(self):
        """إنشاء قاعدة البيانات والجداول"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # جدول الصفقات
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_address TEXT NOT NULL,
                    token_name TEXT NOT NULL,
                    token_symbol TEXT NOT NULL,
                    buy_amount_sol REAL NOT NULL,
                    buy_price_usd REAL NOT NULL,
                    tokens_amount REAL NOT NULL,
                    take_profit REAL,
                    stop_loss REAL,
                    trailing_stop REAL,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP,
                    pnl_usd REAL DEFAULT 0.0
                )
            ''')
            
            # جدول الإحصائيات
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trading_stats (
                    id INTEGER PRIMARY KEY,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0.0,
                    best_trade REAL DEFAULT 0.0,
                    worst_trade REAL DEFAULT 0.0,
                    win_rate REAL DEFAULT 0.0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # جدول التنبيهات
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS price_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_address TEXT NOT NULL,
                    target_price REAL NOT NULL,
                    alert_type TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            conn.commit()
    
    def save_position(self, position: Position) -> int:
        """حفظ صفقة جديدة"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO positions (token_address, token_name, token_symbol, 
                                     buy_amount_sol, buy_price_usd, tokens_amount,
                                     take_profit, stop_loss, trailing_stop, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (position.token_address, position.token_name, position.token_symbol,
                  position.buy_amount_sol, position.buy_price_usd, position.tokens_amount,
                  position.take_profit, position.stop_loss, position.trailing_stop,
                  position.status.value))
            return cursor.lastrowid
    
    def get_active_positions(self) -> List[Position]:
        """جلب الصفقات النشطة"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM positions WHERE status = 'active'
                ORDER BY created_at DESC
            ''')
            
            positions = []
            for row in cursor.fetchall():
                position = Position(
                    token_address=row[1],
                    token_name=row[2], 
                    token_symbol=row[3],
                    buy_amount_sol=row[4],
                    buy_price_usd=row[5],
                    tokens_amount=row[6],
                    take_profit=row[7],
                    stop_loss=row[8],
                    trailing_stop=row[9],
                    status=TradingStatus(row[10]),
                    created_at=datetime.fromisoformat(row[11]),
                    closed_at=datetime.fromisoformat(row[12]) if row[12] else None,
                    pnl_usd=row[13]
                )
                positions.append(position)
            
            return positions
    
    def update_position(self, token_address: str, **kwargs):
        """تحديث صفقة"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            set_clause = ', '.join([f"{k} = ?" for k in kwargs.keys()])
            values = list(kwargs.values()) + [token_address]
            
            cursor.execute(f'''
                UPDATE positions SET {set_clause}
                WHERE token_address = ? AND status = 'active'
            ''', values)
    
    def close_position(self, token_address: str, pnl_usd: float):
        """إغلاق صفقة"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE positions 
                SET status = 'closed', closed_at = CURRENT_TIMESTAMP, pnl_usd = ?
                WHERE token_address = ? AND status = 'active'
            ''', (pnl_usd, token_address))
    
    def get_trading_stats(self) -> TradingStats:
        """جلب الإحصائيات"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM trading_stats WHERE id = 1')
            row = cursor.fetchone()
            
            if row:
                return TradingStats(
                    total_trades=row[1],
                    winning_trades=row[2],
                    losing_trades=row[3],
                    total_pnl=row[4],
                    best_trade=row[5],
                    worst_trade=row[6],
                    win_rate=row[7]
                )
            
            return TradingStats()
    
    def update_trading_stats(self, stats: TradingStats):
        """تحديث الإحصائيات"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO trading_stats 
                (id, total_trades, winning_trades, losing_trades, total_pnl, 
                 best_trade, worst_trade, win_rate, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (stats.total_trades, stats.winning_trades, stats.losing_trades,
                  stats.total_pnl, stats.best_trade, stats.worst_trade, stats.win_rate))

# ——————————— خدمات API المحسنة ———————————
class EnhancedSolanaService:
    def __init__(self, config: Config):
        self.config = config
        self.rpc_index = 0
        self._session = None
    
    async def get_session(self):
        """إنشاء جلسة HTTP"""
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.config.REQUEST_TIMEOUT)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def close_session(self):
        """إغلاق جلسة HTTP"""
        if self._session:
            await self._session.close()
            self._session = None
    
    def _get_next_rpc_url(self) -> str:
        """الحصول على RPC URL التالي (Load Balancing)"""
        url = self.config.SOLANA_RPC_ENDPOINTS[self.rpc_index]
        self.rpc_index = (self.rpc_index + 1) % len(self.config.SOLANA_RPC_ENDPOINTS)
        return url
    
    async def get_sol_balance(self, public_key: str) -> float:
        """جلب رصيد SOL مع إعادة المحاولة المحسنة"""
        session = await self.get_session()
        
        for attempt in range(self.config.MAX_RETRY_ATTEMPTS):
            rpc_url = self._get_next_rpc_url()
            
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [public_key, {"commitment": "finalized"}]
                }
                
                async with session.post(rpc_url, json=payload) as response:
                    response.raise_for_status()
                    data = await response.json()
                    
                    if "result" in data and "value" in data["result"]:
                        lamports = data["result"]["value"]
                        return lamports / 1e9
                        
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed for {rpc_url}: {e}")
                if attempt < self.config.MAX_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(1)
                continue
        
        logger.error("Failed to get SOL balance from all RPC endpoints")
        return 0.0
    
    async def get_token_balance(self, public_key: str, token_mint: str) -> float:
        """جلب رصيد توكن معين"""
        session = await self.get_session()
        
        for attempt in range(self.config.MAX_RETRY_ATTEMPTS):
            rpc_url = self._get_next_rpc_url()
            
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        public_key,
                        {"mint": token_mint},
                        {"encoding": "jsonParsed"}
                    ]
                }
                
                async with session.post(rpc_url, json=payload) as response:
                    response.raise_for_status()
                    data = await response.json()
                    
                    if "result" in data and data["result"]["value"]:
                        account = data["result"]["value"][0]
                        token_amount = account["account"]["data"]["parsed"]["info"]["tokenAmount"]
                        return float(token_amount["uiAmount"] or 0)
                    
                    return 0.0
                        
            except Exception as e:
                logger.warning(f"Token balance attempt {attempt + 1} failed: {e}")
                if attempt < self.config.MAX_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(1)
                continue
        
        return 0.0

# ——————————— خدمة الأسعار المحسنة ———————————
class EnhancedPriceService:
    def __init__(self, config: Config):
        self.config = config
        self._session = None
        self._cache = {}
        self._cache_timeout = 30  # ثانية
    
    async def get_session(self):
        """إنشاء جلسة HTTP"""
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.config.REQUEST_TIMEOUT)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def close_session(self):
        """إغلاق جلسة HTTP"""
        if self._session:
            await self._session.close()
            self._session = None
    
    def _is_cache_valid(self, key: str) -> bool:
        """فحص صحة التخزين المؤقت"""
        if key not in self._cache:
            return False
        
        cache_time = self._cache[key].get('timestamp', 0)
        return time.time() - cache_time < self._cache_timeout
    
    async def get_sol_price_usd(self) -> float:
        """جلب سعر SOL/USD مع التخزين المؤقت"""
        cache_key = "sol_price"
        
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]['price']
        
        session = await self.get_session()
        
        try:
            params = {"ids": "solana", "vs_currencies": "usd"}
            async with session.get(self.config.COINGECKO_API, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                price = float(data.get("solana", {}).get("usd", 0.0))
                
                # تخزين مؤقت
                self._cache[cache_key] = {
                    'price': price,
                    'timestamp': time.time()
                }
                
                return price
                
        except Exception as e:
            logger.error(f"Failed to get SOL price: {e}")
            # إرجاع القيمة المخزنة مؤقتاً إذا توفرت
            if cache_key in self._cache:
                return self._cache[cache_key]['price']
            return 0.0
    
    async def get_enhanced_token_info(self, token_address: str) -> TokenInfo:
        """جلب معلومات التوكن المحسنة"""
        cache_key = f"token_{token_address}"
        
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]['info']
        
        session = await self.get_session()
        
        try:
            url = f"{self.config.DEXSCREENER_API}/{token_address}"
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
                
                pair = data.get("pair", {})
                if not pair:
                    raise ValueError("No pair data found")
                
                base_token = pair.get("baseToken", {})
                token_name = base_token.get("name", "Unknown")
                token_symbol = base_token.get("symbol", token_address[:8])
                
                price_usd = float(pair.get("priceUsd", 0.0))
                price_sol = float(pair.get("priceNative", 0.0))
                liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0.0))
                market_cap_usd = float(pair.get("marketCapUsd", 0.0) or 0.0)
                fdv_usd = float(pair.get("fdvUsd", 0.0) or 0.0)
                volume_24h = float(pair.get("volume", {}).get("h24", 0.0) or 0.0)
                price_change_24h = float(pair.get("priceChange", {}).get("h24", 0.0) or 0.0)
                
                # استخدام FDV إذا كانت القيمة السوقية صفر
                if market_cap_usd <= 0 and fdv_usd > 0:
                    market_cap_usd = fdv_usd
                
                token_info = TokenInfo(
                    token_name=token_name,
                    token_symbol=token_symbol,
                    price_usd=price_usd,
                    price_sol=price_sol,
                    liquidity_usd=liquidity_usd,
                    market_cap_usd=market_cap_usd,
                    fdv_usd=fdv_usd,
                    volume_24h=volume_24h,
                    price_change_24h=price_change_24h
                )
                
                # تخزين مؤقت
                self._cache[cache_key] = {
                    'info': token_info,
                    'timestamp': time.time()
                }
                
                return token_info
                
        except Exception as e:
            logger.error(f"Failed to get token info for {token_address}: {e}")
            return TokenInfo(
                token_name=token_address[:8],
                token_symbol=token_address[:8],
                price_usd=0.0,
                price_sol=0.0,
                liquidity_usd=0.0,
                market_cap_usd=0.0
            )

# ——————————— مساعدات التنسيق المحسنة ———————————
class EnhancedFormatUtils:
    @staticmethod
    def human_format_number(num: Union[int, float]) -> str:
        """تحويل الأرقام إلى صيغة قابلة للقراءة مع دعم الأرقام الكبيرة"""
        try:
            num = float(num)
        except (ValueError, TypeError):
            return "0"
        
        if abs(num) >= 1_000_000_000_000:  # Trillion
            return f"{num/1_000_000_000_000:.2f}T"
        elif abs(num) >= 1_000_000_000:  # Billion
            return f"{num/1_000_000_000:.2f}B"
        elif abs(num) >= 1_000_000:  # Million
            return f"{num/1_000_000:.2f}M"
        elif abs(num) >= 1_000:  # Thousand
            return f"{num/1_000:.2f}K"
        else:
            return f"{num:.2f}"
    
    @staticmethod
    def format_price(price: float, decimals: int = None) -> str:
        """تنسيق السعر مع عدد العشريات المناسب تلقائياً"""
        if price == 0:
            return "0"
        
        # تحديد عدد العشريات تلقائياً
        if decimals is None:
            if price >= 1000:
                decimals = 2
            elif price >= 1:
                decimals = 4
            elif price >= 0.01:
                decimals = 6
            elif price >= 0.000001:
                decimals = 8
            else:
                decimals = 12
        
        formatted = f"{price:.{decimals}f}"
        
        # إزالة الأصفار غير الضرورية
        if '.' in formatted:
            formatted = formatted.rstrip('0').rstrip('.')
        
        return formatted
    
    @staticmethod
    def format_percentage(value: float) -> str:
        """تنسيق النسبة المئوية مع الألوان"""
        if value > 0:
            return f"📈 +{value:.2f}%"
        elif value < 0:
            return f"📉 {value:.2f}%"
        else:
            return "📊 0.00%"
    
    @staticmethod
    def format_duration(start_time: datetime, end_time: datetime = None) -> str:
        """تنسيق المدة الزمنية"""
        if end_time is None:
            end_time = datetime.now()
        
        duration = end_time - start_time
        
        if duration.days > 0:
            return f"{duration.days}د {duration.seconds // 3600}س"
        elif duration.seconds >= 3600:
            hours = duration.seconds // 3600
            minutes = (duration.seconds % 3600) // 60
            return f"{hours}س {minutes}ق"
        elif duration.seconds >= 60:
            minutes = duration.seconds // 60
            return f"{minutes}ق"
        else:
            return "< 1ق"
    
    @staticmethod
    def format_pnl(pnl: float, show_currency: bool = True) -> str:
        """تنسيق الربح/الخسارة مع الرموز التعبيرية"""
        currency = "$" if show_currency else ""
        
        if pnl > 0:
            return f"🟢 +{currency}{pnl:.2f}"
        elif pnl < 0:
            return f"🔴 {currency}{pnl:.2f}"
        else:
            return f"⚪ {currency}0.00"

# ——————————— واجهات المستخدم المحسنة ———————————
class EnhancedUIBuilder:
    @staticmethod
    def build_main_menu() -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton("💰 Trade", callback_data="MENU_TRADE"),
                InlineKeyboardButton("📊 Portfolio", callback_data="MENU_POSITIONS")
            ],
            [
                InlineKeyboardButton("📈 Analytics", callback_data="MENU_ANALYTICS"),
                InlineKeyboardButton("💳 Wallet", callback_data="MENU_WALLET")
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="MENU_SETTINGS"),
                InlineKeyboardButton("📱 Quick Actions", callback_data="MENU_QUICK")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def build_trade_menu() -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton("🟢 Buy Token", callback_data="TRADE_BUY"),
                InlineKeyboardButton("🔴 Sell Token", callback_data="TRADE_SELL")
            ],
            [
                InlineKeyboardButton("📊 Quick Buy", callback_data="QUICK_BUY"),
                InlineKeyboardButton("⚡ Market Orders", callback_data="MARKET_ORDERS")
            ],
            [
                InlineKeyboardButton("🔙 Back", callback_data="BACK_MAIN")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def build_analytics_menu() -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton("📊 Trading Stats", callback_data="ANALYTICS_STATS"),
                InlineKeyboardButton("📈 Performance", callback_data="ANALYTICS_PERFORMANCE")
            ],
            [
                InlineKeyboardButton("🎯 Win Rate", callback_data="ANALYTICS_WINRATE"),
                InlineKeyboardButton("💰 P&L History", callback_data="ANALYTICS_PNL")
            ],
            [
                InlineKeyboardButton("🔙 Back", callback_data="BACK_MAIN")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def build_enhanced_buy_amounts() -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton("💎 0.01 SOL", callback_data="BUY_AMOUNT_0.01"),
                InlineKeyboardButton("💰 0.05 SOL", callback_data="BUY_AMOUNT_0.05")
            ],
            [
                InlineKeyboardButton("🚀 0.1 SOL", callback_data="BUY_AMOUNT_0.1"),
                InlineKeyboardButton("💸 0.25 SOL", callback_data="BUY_AMOUNT_0.25")
            ],
            [
                InlineKeyboardButton("🔥 0.5 SOL", callback_data="BUY_AMOUNT_0.5"),
                InlineKeyboardButton("⚡ 1.0 SOL", callback_data="BUY_AMOUNT_1.0")
            ],
            [
                
                InlineKeyboardButton("💵 Custom Amount", callback_data="BUY_AMOUNT_CUSTOM"),
                InlineKeyboardButton("🔙 Back", callback_data="BACK_TRADE")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def build_position_actions(token_address: str) -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton("📊 View Details", callback_data=f"POSITION_DETAILS_{token_address}"),
                InlineKeyboardButton("💰 Sell 25%", callback_data=f"SELL_25_{token_address}")
            ],
            [
                InlineKeyboardButton("💸 Sell 50%", callback_data=f"SELL_50_{token_address}"),
                InlineKeyboardButton("🔴 Sell 100%", callback_data=f"SELL_100_{token_address}")
            ],
            [
                InlineKeyboardButton("🎯 Set TP/SL", callback_data=f"SET_TPSL_{token_address}"),
                InlineKeyboardButton("📈 Set Trailing", callback_data=f"SET_TRAILING_{token_address}")
            ],
            [
                InlineKeyboardButton("🔙 Back", callback_data="MENU_POSITIONS")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    @staticmethod
    def build_confirmation_dialog(action: str, amount: str = "") -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton("✅ Confirm", callback_data=f"CONFIRM_{action}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"CANCEL_{action}")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    # إضافة لوحة مفاتيح لتحليل التوكن
    @staticmethod
    def build_token_analysis_actions(token_address: str) -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton("🟢 Buy Now", callback_data=f"BUY_{token_address}"),
                InlineKeyboardButton("🔴 Sell Now", callback_data=f"SELL_{token_address}")
            ],
            [
                InlineKeyboardButton("📊 View Chart", callback_data=f"CHART_{token_address}"),
                InlineKeyboardButton("🔔 Set Alert", callback_data=f"SET_ALERT_{token_address}")
            ],
            [
                InlineKeyboardButton("🔙 Back", callback_data="BACK_TRADE")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    # إضافة لوحة مفاتيح للإعدادات
    @staticmethod
    def build_settings_menu() -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton("💰 Default Buy Amount", callback_data="SETTING_DEFAULT_BUY"),
                InlineKeyboardButton("⏱️ Cooldown Interval", callback_data="SETTING_COOLDOWN")
            ],
            [
                InlineKeyboardButton("🔔 Manage Alerts", callback_data="SETTING_MANAGE_ALERTS"),
                InlineKeyboardButton("🔄 Refresh Settings", callback_data="SETTINGS_REFRESH")
            ],
            [
                InlineKeyboardButton("🔙 Back", callback_data="BACK_MAIN")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

# ——————————— مدير الإشعارات المحسن ———————————
class EnhancedNotificationManager:
    def __init__(self, bot, admin_id: int):
        self.bot = bot
        self.admin_id = admin_id
        self.notification_queue = asyncio.Queue()
        self.is_running = False
    
    async def start(self):
        """بدء معالج الإشعارات"""
        self.is_running = True
        asyncio.create_task(self._process_notifications())
    
    async def stop(self):
        """إيقاف معالج الإشعارات"""
        self.is_running = False
    
    async def _process_notifications(self):
        """معالجة طابور الإشعارات"""
        while self.is_running:
            try:
                notification = await asyncio.wait_for(
                    self.notification_queue.get(), timeout=1.0
                )
                await self._send_notification(notification)
                await asyncio.sleep(0.5)  # تأخير بسيط لتجنب السبام
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Notification processing error: {e}")
    
    async def _send_notification(self, notification: dict):
        """إرسال إشعار"""
        try:
            message = notification.get("message", "")
            keyboard = notification.get("keyboard", None)
            
            await self.bot.send_message(
                chat_id=self.admin_id,
                text=message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")
    
    async def notify_trade_executed(self, position: Position, action: str):
        """إشعار تنفيذ صفقة"""
        price_str = EnhancedFormatUtils.format_price(position.buy_price_usd)
        amount_str = EnhancedFormatUtils.format_price(position.buy_amount_sol)
        
        message = f"""
🎯 <b>Trade Executed</b>

<b>{action.upper()}</b> | {position.token_symbol}
━━━━━━━━━━━━━━━━
💰 Amount: {amount_str} SOL
💵 Price: ${price_str}
🪙 Tokens: {EnhancedFormatUtils.human_format_number(position.tokens_amount)}
📊 Total Value: ${EnhancedFormatUtils.format_price(position.buy_amount_sol * position.buy_price_usd)}
⏰ Time: {position.created_at.strftime('%H:%M:%S')}
        """
        
        keyboard = EnhancedUIBuilder.build_position_actions(position.token_address)
        
        await self.notification_queue.put({
            "message": message,
            "keyboard": keyboard
        })
    
    async def notify_price_alert(self, token_info: TokenInfo, target_price: float, alert_type: str):
        """إشعار تنبيه السعر"""
        current_price = EnhancedFormatUtils.format_price(token_info.price_usd)
        target_price_str = EnhancedFormatUtils.format_price(target_price)
        
        alert_emoji = "🚨" if alert_type == "stop_loss" else "🎯"
        
        message = f"""
{alert_emoji} <b>Price Alert Triggered</b>

<b>{token_info.token_symbol}</b>
━━━━━━━━━━━━━━━━
💰 Current Price: ${current_price}
🎯 Target Price: ${target_price_str}
📊 Alert Type: {alert_type.replace('_', ' ').title()}
⏰ Time: {datetime.now().strftime('%H:%M:%S')}
        """
        
        await self.notification_queue.put({
            "message": message
        })
    
    async def notify_pnl_update(self, position: Position, current_price: float):
        """إشعار تحديث الربح/الخسارة"""
        current_value = position.tokens_amount * current_price
        initial_value = position.buy_amount_sol * position.buy_price_usd
        pnl_usd = current_value - initial_value
        pnl_percentage = (pnl_usd / initial_value) * 100 if initial_value > 0 else 0
        
        # إرسال إشعار فقط للتغييرات الكبيرة
        if abs(pnl_percentage) < 10:
            return
        
        pnl_str = EnhancedFormatUtils.format_pnl(pnl_usd)
        percentage_str = EnhancedFormatUtils.format_percentage(pnl_percentage)
        
        message = f"""
📊 <b>P&L Update</b>

<b>{position.token_symbol}</b>
━━━━━━━━━━━━━━━━
{pnl_str}
{percentage_str}
💰 Current Value: ${EnhancedFormatUtils.format_price(current_value)}
⏰ Duration: {EnhancedFormatUtils.format_duration(position.created_at)}
        """
        
        await self.notification_queue.put({
            "message": message
        })

# ——————————— مراقب الأسعار المحسن ———————————
class EnhancedPriceMonitor:
    def __init__(self, price_service: EnhancedPriceService, 
                 db_manager: DatabaseManager,
                 notification_manager: EnhancedNotificationManager,
                 config: Config):
        self.price_service = price_service
        self.db_manager = db_manager
        self.notification_manager = notification_manager
        self.config = config
        self.is_monitoring = False
        self.last_prices = {}
        self.monitoring_tasks = {}
    
    async def start_monitoring(self):
        """بدء مراقبة الأسعار"""
        if self.is_monitoring:
            return
        
        self.is_monitoring = True
        asyncio.create_task(self._monitor_positions())
        logger.info("Price monitoring started")
    
    async def stop_monitoring(self):
        """إيقاف مراقبة الأسعار"""
        self.is_monitoring = False
        
        # إيقاف جميع مهام المراقبة
        for task in self.monitoring_tasks.values():
            task.cancel()
        
        self.monitoring_tasks.clear()
        logger.info("Price monitoring stopped")
    
    async def _monitor_positions(self):
        """مراقبة الصفقات النشطة"""
        while self.is_monitoring:
            try:
                active_positions = self.db_manager.get_active_positions()
                
                for position in active_positions:
                    if position.token_address not in self.monitoring_tasks:
                        # إنشاء مهمة مراقبة جديدة للتوكن
                        task = asyncio.create_task(
                            self._monitor_single_position(position)
                        )
                        self.monitoring_tasks[position.token_address] = task
                
                # تنظيف المهام المنتهية
                finished_tasks = [
                    addr for addr, task in self.monitoring_tasks.items() 
                    if task.done()
                ]
                
                for addr in finished_tasks:
                    del self.monitoring_tasks[addr]
                
                await asyncio.sleep(self.config.PRICE_MONITOR_INTERVAL)
                
            except Exception as e:
                logger.error(f"Position monitoring error: {e}")
                await asyncio.sleep(self.config.PRICE_MONITOR_INTERVAL)
    
    async def _monitor_single_position(self, position: Position):
        """مراقبة صفقة واحدة"""
        try:
            while self.is_monitoring:
                token_info = await self.price_service.get_enhanced_token_info(
                    position.token_address
                )
                
                if token_info.price_usd <= 0:
                    await asyncio.sleep(self.config.PRICE_MONITOR_INTERVAL)
                    continue
                
                # فحص شروط التوقف
                await self._check_stop_conditions(position, token_info.price_usd)
                
                # تحديث إشعارات الربح/الخسارة
                if self.config.ENABLE_PROFIT_NOTIFICATIONS:
                    await self.notification_manager.notify_pnl_update(
                        position, token_info.price_usd
                    )
                
                # تحديث السعر الأخير
                self.last_prices[position.token_address] = token_info.price_usd
                
                await asyncio.sleep(self.config.PRICE_MONITOR_INTERVAL)
                
        except asyncio.CancelledError:
            logger.info(f"Monitoring cancelled for {position.token_symbol}")
        except Exception as e:
            logger.error(f"Single position monitoring error: {e}")
    
    async def _check_stop_conditions(self, position: Position, current_price: float):
        """فحص شروط التوقف (TP/SL)"""
        should_close = False
        close_reason = ""
        
        # فحص Take Profit
        if position.take_profit and current_price >= position.take_profit:
            should_close = True
            close_reason = "Take Profit"
        
        # فحص Stop Loss
        elif position.stop_loss and current_price <= position.stop_loss:
            should_close = True
            close_reason = "Stop Loss"
        
        # فحص Trailing Stop
        elif position.trailing_stop:
            last_price = self.last_prices.get(position.token_address, position.buy_price_usd)
            if current_price <= last_price * (1 - position.trailing_stop / 100):
                should_close = True
                close_reason = "Trailing Stop"
        
        if should_close:
            await self._execute_auto_close(position, current_price, close_reason)
    
    async def _execute_auto_close(self, position: Position, current_price: float, reason: str):
        """تنفيذ الإغلاق التلقائي"""
        try:
            # حساب الربح/الخسارة
            current_value = position.tokens_amount * current_price
            initial_value = position.buy_amount_sol * position.buy_price_usd
            pnl_usd = current_value - initial_value
            
            # تحديث قاعدة البيانات
            self.db_manager.close_position(position.token_address, pnl_usd)
            
            # تحديث الإحصائيات
            stats = self.db_manager.get_trading_stats()
            stats.update_stats(pnl_usd)
            self.db_manager.update_trading_stats(stats)
            
            # إرسال إشعار
            await self._notify_auto_close(position, current_price, pnl_usd, reason)
            
            logger.info(f"Auto-closed position {position.token_symbol} - {reason}")
            
        except Exception as e:
            logger.error(f"Auto-close execution error: {e}")
    
    async def _notify_auto_close(self, position: Position, close_price: float, 
                                pnl_usd: float, reason: str):
        """إشعار الإغلاق التلقائي"""
        pnl_str = EnhancedFormatUtils.format_pnl(pnl_usd)
        duration = EnhancedFormatUtils.format_duration(position.created_at)
        
        emoji = "🎯" if reason == "Take Profit" else "🛑"
        
        message = f"""
{emoji} <b>Position Auto-Closed</b>

<b>{position.token_symbol}</b>
━━━━━━━━━━━━━━━━
📊 Reason: {reason}
💰 Entry: ${EnhancedFormatUtils.format_price(position.buy_price_usd)}
💸 Exit: ${EnhancedFormatUtils.format_price(close_price)}
{pnl_str}
⏰ Duration: {duration}
        """
        
        await self.notification_manager.notification_queue.put({
            "message": message
        })

# ——————————— البوت الرئيسي المحسن ———————————
class EnhancedTradingBot:
    def __init__(self):
        self.config = Config()
        if not self.config.validate():
            raise ValueError("Invalid configuration")
        
        self.db_manager = DatabaseManager(self.config.DATABASE_PATH)
        self.solana_service = EnhancedSolanaService(self.config)
        self.price_service = EnhancedPriceService(self.config)
        
        self.app = None
        self.notification_manager = None
        self.price_monitor = None
        
        # متغيرات حالة المستخدم
        self.user_states = {}
        self.pending_trades = {}
        
        # إحصائيات الأداء
        self.performance_stats = {
            'start_time': datetime.now(),
            'commands_processed': 0,
            'trades_executed': 0,
            'errors_count': 0
        }
    
    async def initialize(self):
        """تهيئة البوت"""
        try:
            # إنشاء التطبيق
            self.app = ApplicationBuilder().token(self.config.BOT_TOKEN).build()
            
            # تهيئة مدير الإشعارات
            self.notification_manager = EnhancedNotificationManager(
                self.app.bot, self.config.ADMIN_ID
            )
            
            # تهيئة مراقب الأسعار
            self.price_monitor = EnhancedPriceMonitor(
                self.price_service,
                self.db_manager,
                self.notification_manager,
                self.config
            )
            
            # تسجيل معالجات الأوامر
            await self._register_handlers()
            
            # تعيين أوامر البوت
            await self._set_bot_commands()
            
            logger.info("Bot initialized successfully")
            
        except Exception as e:
            logger.error(f"Bot initialization failed: {e}")
            raise
    
    async def _register_handlers(self):
        """تسجيل معالجات الأوامر والرسائل"""
        # الأوامر الأساسية
        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("menu", self._handle_menu))
        self.app.add_handler(CommandHandler("wallet", self._handle_wallet))
        self.app.add_handler(CommandHandler("positions", self._handle_positions))
        self.app.add_handler(CommandHandler("stats", self._handle_stats))
        self.app.add_handler(CommandHandler("buy", self._handle_buy_command))
        self.app.add_handler(CommandHandler("sell", self._handle_sell_command))
        self.app.add_handler(CommandHandler("help", self._handle_help))
        
        # معالج الرسائل النصية
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, 
            self._handle_text_message
        ))
        
        # معالج الاستعلامات المضمنة
        self.app.add_handler(CallbackQueryHandler(self._handle_callback_query))
        
        # معالج الأخطاء
        self.app.add_error_handler(self._handle_error)
    
    async def _set_bot_commands(self):
        """تعيين أوامر البوت"""
        commands = [
            BotCommand("start", "🚀 Start the bot"),
            BotCommand("menu", "📱 Main menu"),
            BotCommand("wallet", "💳 Wallet info"),
            BotCommand("positions", "📊 Active positions"),
            BotCommand("stats", "📈 Trading statistics"),
            BotCommand("buy", "🟢 Buy token"),
            BotCommand("sell", "🔴 Sell token"),
            BotCommand("help", "❓ Help & commands")
        ]
        
        await self.app.bot.set_my_commands(commands)
    
    async def start(self):
        """بدء البوت"""
        try:
            await self.initialize()
            
            # بدء خدمات الخلفية
            await self.notification_manager.start()
            await self.price_monitor.start_monitoring()
            
            # بدء البوت
            await self.app.initialize()
            await self.app.start()
            await self.app.updater.start_polling()
            
            logger.info("Bot started successfully")
            
            # إبقاء البوت يعمل
            while True:
                await asyncio.sleep(1)
                
        except KeyboardInterrupt:
            logger.info("Bot shutdown requested")
        except Exception as e:
            logger.error(f"Bot startup error: {e}")
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """إيقاف البوت"""
        try:
            if self.price_monitor:
                await self.price_monitor.stop_monitoring()
            
            if self.notification_manager:
                await self.notification_manager.stop()
            
            if self.solana_service:
                await self.solana_service.close_session()
            
            if self.price_service:
                await self.price_service.close_session()
            
            if self.app:
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            
            logger.info("Bot shutdown completed")
            
        except Exception as e:
            logger.error(f"Shutdown error: {e}")
    
    # ——————————— معالجات الأوامر ———————————
    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالج أمر البدء"""
        try:
            user_id = update.effective_user.id
            
            if user_id != self.config.ADMIN_ID:
                await update.message.reply_text(
                    "❌ Unauthorized access. This bot is private."
                )
                return
            
            welcome_message = f"""
🚀 <b>Welcome to Enhanced Trading Bot</b>

<b>💰 Solana DeFi Trading Assistant</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔹 <b>Features:</b>
• 🔥 Advanced Trading Engine
• 📊 Real-time Portfolio Tracking  
• 🎯 Automated TP/SL Management
• 📈 Comprehensive Analytics
• ⚡ Lightning-fast Execution
• 🛡️ Enhanced Security

🔹 <b>Commands:</b>
• /menu - Main interface
• /wallet - Wallet information
• /positions - Active positions
• /stats - Trading statistics
• /buy - Quick buy tokens
• /sell - Quick sell tokens

<b>⚠️ Risk Warning:</b>
Cryptocurrency trading involves high risk. 
Only trade with funds you can afford to lose.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ready to start trading? 🚀
            """
            
            keyboard = EnhancedUIBuilder.build_main_menu()
            
            await update.message.reply_text(
                welcome_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
            self.performance_stats['commands_processed'] += 1
            
        except Exception as e:
            logger.error(f"Start command error: {e}")
            await update.message.reply_text("❌ Error occurred. Please try again.")
    
    async def _handle_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالج قائمة البوت الرئيسية"""
        try:
            if not await self._check_authorization(update):
                return
            
            # جلب معلومات الحساب
            sol_balance = await self.solana_service.get_sol_balance(self.config.PUBLIC_KEY)
            sol_price = await self.price_service.get_sol_price_usd()
            usd_balance = sol_balance * sol_price
            
            # جلب الصفقات النشطة
            active_positions = self.db_manager.get_active_positions()
            
            # جلب الإحصائيات
            stats = self.db_manager.get_trading_stats()
            
            menu_message = f"""
📱 <b>Trading Dashboard</b>

💳 <b>Wallet</b>
━━━━━━━━━━━━━━━━
💰 SOL: {EnhancedFormatUtils.format_price(sol_balance)} 
💵 USD: ${EnhancedFormatUtils.format_price(usd_balance)}

📊 <b>Portfolio</b>
━━━━━━━━━━━━━━━━
🔥 Active Positions: {len(active_positions)}
📈 Total P&L: {EnhancedFormatUtils.format_pnl(stats.total_pnl)}
🎯 Win Rate: {stats.win_rate:.1f}%

⚡ <b>Quick Stats</b>
━━━━━━━━━━━━━━━━
📊 Total Trades: {stats.total_trades}
🟢 Winning: {stats.winning_trades}
🔴 Losing: {stats.losing_trades}

Choose an option below:
            """
            
            keyboard = EnhancedUIBuilder.build_main_menu()
            
            await update.message.reply_text(
                menu_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
            self.performance_stats['commands_processed'] += 1
            
        except Exception as e:
            logger.error(f"Menu command error: {e}")
            await update.message.reply_text("❌ Error loading menu. Please try again.")
    
    async def _handle_wallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالج معلومات المحفظة"""
        try:
            if not await self._check_authorization(update):
                return
            
            # جلب رصيد SOL
            sol_balance = await self.solana_service.get_sol_balance(self.config.PUBLIC_KEY)
            sol_price = await self.price_service.get_sol_price_usd()
            usd_balance = sol_balance * sol_price
            
            # جلب الصفقات النشطة لحساب القيمة الإجمالية
            active_positions = self.db_manager.get_active_positions()
            total_portfolio_value = usd_balance
            
            for position in active_positions:
                try:
                    token_info = await self.price_service.get_enhanced_token_info(
                        position.token_address
                    )
                    position_value = position.tokens_amount * token_info.price_usd
                    total_portfolio_value += position_value
                except Exception:
                    continue
            
            wallet_message = f"""
💳 <b>Wallet Overview</b>

💰 <b>SOL Balance</b>
━━━━━━━━━━━━━━━━
🪙 Amount: {EnhancedFormatUtils.format_price(sol_balance)} SOL
💵 USD Value: ${EnhancedFormatUtils.format_price(usd_balance)}
📊 SOL Price: ${EnhancedFormatUtils.format_price(sol_price)}

📊 <b>Portfolio Summary</b>
━━━━━━━━━━━━━━━━
💎 Total Value: ${EnhancedFormatUtils.format_price(total_portfolio_value)}
🔥 Active Positions: {len(active_positions)}
💰 Available SOL: {EnhancedFormatUtils.format_price(sol_balance)}

🏦 <b>Account Details</b>
━━━━━━━━━━━━━━━━
🔑 Address: <code>{self.config.PUBLIC_KEY[:12]}...{self.config.PUBLIC_KEY[-12:]}</code>
⏰ Last Update: {datetime.now().strftime('%H:%M:%S')}

<i>Tap address to copy</i>
            """
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="WALLET_REFRESH")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="BACK_MAIN")]
            ])
            
            await update.message.reply_text(
                wallet_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
            self.performance_stats['commands_processed'] += 1
            
        except Exception as e:
            logger.error(f"Wallet command error: {e}")
            await update.message.reply_text("❌ Error loading wallet info. Please try again.")
    
    async def _handle_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالج الصفقات النشطة"""
        try:
            if not await self._check_authorization(update):
                return
            
            active_positions = self.db_manager.get_active_positions()
            
            if not active_positions:
                message = """
📊 <b>Active Positions</b>

🔍 No active positions found.
Ready to start trading?

Use /buy to open your first position!
                """
                
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🟢 Start Trading", callback_data="MENU_TRADE")],
                    [InlineKeyboardButton("🔙 Back to Menu", callback_data="BACK_MAIN")]
                ])
                
                await update.message.reply_text(
                    message,
                    reply_markup=keyboard,
                    parse_mode='HTML'
                )
                return
            
            # عرض الصفقات النشطة
            positions_text = "📊 <b>Active Positions</b>\n\n"
            total_pnl = 0
            
            for i, position in enumerate(active_positions[:10], 1):  # أول 10 صفقات
                try:
                    token_info = await self.price_service.get_enhanced_token_info(
                        position.token_address
                    )
                    
                    current_value = position.tokens_amount * token_info.price_usd
                    initial_value = position.buy_amount_sol * position.buy_price_usd
                    pnl_usd = current_value - initial_value
                    pnl_percentage = (pnl_usd / initial_value) * 100 if initial_value > 0 else 0
                    
                    total_pnl += pnl_usd
                    
                    duration = EnhancedFormatUtils.format_duration(position.created_at)
                    pnl_str = EnhancedFormatUtils.format_pnl(pnl_usd)
                    percentage_str = EnhancedFormatUtils.format_percentage(pnl_percentage)
                    
                    positions_text += f"""
<b>{i}. {position.token_symbol}</b>
💰 Entry: ${EnhancedFormatUtils.format_price(position.buy_price_usd)}
📊 Current: ${EnhancedFormatUtils.format_price(token_info.price_usd)}
{pnl_str} ({percentage_str})
⏰ {duration}
━━━━━━━━━━━━━━━━
"""
                except Exception as e:
                    logger.error(f"Error processing position {position.token_symbol}: {e}")
                    continue
            
            # إضافة ملخص إجمالي
            positions_text += f"""
📈 <b>Total P&L: {EnhancedFormatUtils.format_pnl(total_pnl)}</b>
🔥 Positions: {len(active_positions)}
            """
            # إنشاء لوحة مفاتيح للصفقات
            keyboard_buttons = []
            for position in active_positions[:5]:  # أول 5 صفقات للأزرار
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        f"📊 {position.token_symbol}",
                        callback_data=f"POSITION_DETAILS_{position.token_address}"
                    )
                ])
            
            keyboard_buttons.extend([
                [InlineKeyboardButton("🔄 Refresh", callback_data="POSITIONS_REFRESH")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="BACK_MAIN")]
            ])
            
            keyboard = InlineKeyboardMarkup(keyboard_buttons)
            
            await update.message.reply_text(
                positions_text,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
            self.performance_stats['commands_processed'] += 1
            
        except Exception as e:
            logger.error(f"Positions command error: {e}")
            await update.message.reply_text("❌ Error loading positions. Please try again.")
    
    async def _handle_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالج إحصائيات التداول"""
        try:
            if not await self._check_authorization(update):
                return
            
            stats = self.db_manager.get_trading_stats()
            active_positions = self.db_manager.get_active_positions()
            
            # حساب P&L الحالي للصفقات النشطة
            current_unrealized_pnl = 0
            for position in active_positions:
                try:
                    token_info = await self.price_service.get_enhanced_token_info(
                        position.token_address
                    )
                    current_value = position.tokens_amount * token_info.price_usd
                    initial_value = position.buy_amount_sol * position.buy_price_usd
                    current_unrealized_pnl += (current_value - initial_value)
                except Exception:
                    continue
            
            # حساب معدلات الأداء
            uptime = datetime.now() - self.performance_stats['start_time']
            uptime_str = EnhancedFormatUtils.format_duration(uptime)
            
            stats_message = f"""
📈 <b>Trading Statistics</b>

💰 <b>Performance Overview</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 Total P&L: {EnhancedFormatUtils.format_pnl(stats.total_pnl)}
💎 Unrealized P&L: {EnhancedFormatUtils.format_pnl(current_unrealized_pnl)}
🎯 Win Rate: {stats.win_rate:.1f}%
🔥 Active Positions: {len(active_positions)}

📊 <b>Trade Statistics</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📈 Total Trades: {stats.total_trades}
🟢 Winning Trades: {stats.winning_trades}
🔴 Losing Trades: {stats.losing_trades}
💰 Best Trade: {EnhancedFormatUtils.format_pnl(stats.best_trade)}
📉 Worst Trade: {EnhancedFormatUtils.format_pnl(stats.worst_trade)}

⚡ <b>System Performance</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏰ Uptime: {uptime_str}
🔧 Commands: {self.performance_stats['commands_processed']}
💼 Trades Executed: {self.performance_stats['trades_executed']}
⚠️ Errors: {self.performance_stats['errors_count']}

📅 <b>Session Info</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚀 Started: {self.performance_stats['start_time'].strftime('%Y-%m-%d %H:%M')}
🔄 Last Update: {datetime.now().strftime('%H:%M:%S')}
            """
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📊 Details", callback_data="ANALYTICS_STATS"),
                    InlineKeyboardButton("🔄 Refresh", callback_data="STATS_REFRESH")
                ],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="BACK_MAIN")]
            ])
            
            await update.message.reply_text(
                stats_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
            self.performance_stats['commands_processed'] += 1
            
        except Exception as e:
            logger.error(f"Stats command error: {e}")
            await update.message.reply_text("❌ Error loading statistics. Please try again.")
    
    async def _handle_buy_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالج أمر الشراء السريع"""
        try:
            if not await self._check_authorization(update):
                return
            
            args = context.args
            if not args:
                await update.message.reply_text(
                    "💡 Usage: /buy <token_address> [amount_sol]\n"
                    "Example: /buy EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v 0.1"
                )
                return
            
            token_address = args[0]
            amount_sol = float(args[1]) if len(args) > 1 else self.config.DEFAULT_BUY_AMOUNT_SOL
            
            # التحقق من صحة العنوان
            if not self._is_valid_solana_address(token_address):
                await update.message.reply_text("❌ Invalid token address format.")
                return
            
            # تنفيذ الصفقة
            await self._execute_buy_trade(update, token_address, amount_sol)
            
        except ValueError:
            await update.message.reply_text("❌ Invalid amount format.")
        except Exception as e:
            logger.error(f"Buy command error: {e}")
            await update.message.reply_text("❌ Error processing buy command.")
    
    async def _handle_sell_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالج أمر البيع السريع"""
        try:
            if not await self._check_authorization(update):
                return
            
            args = context.args
            if not args:
                await update.message.reply_text(
                    "💡 Usage: /sell <token_address> [percentage]\n"
                    "Example: /sell EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v 50"
                )
                return
            
            token_address = args[0]
            percentage = float(args[1]) if len(args) > 1 else 100.0
            
            # التحقق من النسبة المئوية
            if not (0 < percentage <= 100):
                await update.message.reply_text("❌ Percentage must be between 0 and 100.")
                return
            
            # تنفيذ عملية البيع
            await self._execute_sell_trade(update, token_address, percentage)
            
        except ValueError:
            await update.message.reply_text("❌ Invalid percentage format.")
        except Exception as e:
            logger.error(f"Sell command error: {e}")
            await update.message.reply_text("❌ Error processing sell command.")
    
    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالج أمر المساعدة"""
        try:
            if not await self._check_authorization(update):
                return
            
            help_message = """
❓ <b>Bot Commands & Features</b>

<b>📱 Main Commands</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
/start - Initialize the bot
/menu - Open main dashboard
/wallet - View wallet information
/positions - Show active positions
/stats - Trading statistics
/help - This help message

<b>🔥 Trading Commands</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
/buy <address> [amount] - Quick buy
/sell <address> [%] - Quick sell

<b>💡 Examples</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<code>/buy EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v 0.1</code>
<code>/sell EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v 50</code>

<b>🎯 Features</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• ⚡ Lightning-fast execution
• 📊 Real-time price monitoring
• 🎯 Automated TP/SL orders
• 📈 Advanced analytics
• 🔔 Smart notifications
• 🛡️ Enhanced security

<b>⚠️ Important Notes</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Always verify token addresses
• Start with small amounts
• Set stop-loss orders
• Monitor your positions
• Cryptocurrency trading is high risk

<b>🆘 Support</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
If you encounter issues, use /menu to restart
or contact support through the main interface.
            """
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📱 Open Menu", callback_data="BACK_MAIN")],
                [InlineKeyboardButton("🔄 Refresh", callback_data="HELP_REFRESH")]
            ])
            
            await update.message.reply_text(
                help_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
            self.performance_stats['commands_processed'] += 1
            
        except Exception as e:
            logger.error(f"Help command error: {e}")
            await update.message.reply_text("❌ Error loading help. Please try again.")
    
    async def _handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالج الرسائل النصية"""
        try:
            if not await self._check_authorization(update):
                return
            
            user_id = update.effective_user.id
            message_text = update.message.text.strip()
            
            # التحقق من حالة المستخدم
            user_state = self.user_states.get(user_id, {})
            current_state = user_state.get('state', None)
            
            if current_state == 'WAITING_TOKEN_ADDRESS':
                await self._handle_token_address_input(update, message_text)
            elif current_state == 'WAITING_BUY_AMOUNT':
                await self._handle_buy_amount_input(update, message_text)
            elif current_state == 'WAITING_SELL_PERCENTAGE':
                await self._handle_sell_percentage_input(update, message_text)
            elif current_state == 'WAITING_CUSTOM_AMOUNT':
                await self._handle_custom_amount_input(update, message_text)
            elif self._is_valid_solana_address(message_text):
                # إذا كان النص عنوان توكن صحيح
                await self._handle_token_address_input(update, message_text)
            else:
                # رسالة افتراضية
                await update.message.reply_text(
                    "💡 Send a token address to analyze, or use /menu for options.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📱 Open Menu", callback_data="BACK_MAIN")]
                    ])
                )
            
        except Exception as e:
            logger.error(f"Text message error: {e}")
            await update.message.reply_text("❌ Error processing message.")
    
    async def _handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالج الاستعلامات المضمنة (الأزرار)"""
        try:
            query = update.callback_query
            await query.answer()
            
            if not await self._check_authorization(update):
                return
            
            data = query.data
            user_id = update.effective_user.id
            
# معالجة الأوامر المختلفة
        try:
            if data == "BACK_MAIN":
                await self._show_main_menu(query)
            elif data == "MENU_TRADE":
                await self._show_trade_menu(query)
            elif data == "MENU_POSITIONS":
                await self._show_positions_menu(query)
            elif data == "MENU_WALLET":
                await self._show_wallet_info(query)
            elif data == "MENU_ANALYTICS":
                await self._show_analytics_menu(query)
            elif data == "MENU_SETTINGS":
                await self._show_settings_menu(query)
            elif data == "MENU_QUICK":
                await self._show_quick_actions_menu(query)
            
            # التحليلات والإحصائيات
            elif data == "ANALYTICS_STATS":
                await self._handle_stats_from_callback(query)
            elif data == "ANALYTICS_PERFORMANCE":
                await self._show_performance(query)
            elif data == "ANALYTICS_WINRATE":
                await self._show_win_rate(query)
            elif data == "ANALYTICS_PNL":
                await self._show_pnl_history(query)
            
            # الأداء التفصيلي
            elif data == "PERF_DAILY":
                await self._show_performance(query, 'daily')
            elif data == "PERF_MONTHLY":
                await self._show_performance(query, 'monthly')
            
            # التنبيهات والإعدادات
            elif data == "ADD_NEW_ALERT":
                await self._prompt_add_alert(query)
            elif data == "REMOVE_ALERTS":
                await self._remove_alerts(query)
            elif data == "SET_ALERT":
                await self._prompt_add_alert(query)
            elif data == "REFRESH_SETTINGS":
                await self._show_settings_menu(query)
            
            # تحليل الرموز والرسوم البيانية
            elif data == "ANALYZE_TOKEN":
                await self._prompt_token_analysis(query)
            elif data.startswith("CHART_"):
                token_address = data.replace("CHART_", "")
                await self._show_chart(query, token_address)
            
            # عمليات البيع والشراء
            elif data == "SELL_NOW":
                await self._execute_sell_trade(query, token_address, 100)
            elif data == "TRADE_BUY":
                await self._handle_trade_buy(query)
            elif data == "TRADE_SELL":
                await self._handle_trade_sell(query)
            elif data.startswith("BUY_AMOUNT_"):
                await self._handle_buy_amount_selection(query, data)
            elif data.startswith("SELL_"):
                await self._handle_sell_action(query, data)
            
            # تفاصيل المراكز والتأكيدات
            elif data.startswith("POSITION_DETAILS_"):
                await self._show_position_details(query, data)
            elif data.startswith("CONFIRM_"):
                await self._handle_confirmation(query, data)
            elif data.startswith("CANCEL_"):
                await self._handle_cancellation(query, data)
            
            # تحديث البيانات
            elif data == "WALLET_REFRESH":
                await self._refresh_wallet(query)
            elif data == "POSITIONS_REFRESH":
                await self._refresh_positions(query)
            elif data == "STATS_REFRESH":
                await self._refresh_stats(query)
            
            # إعدادات التداول
            elif data == "SETTING_DEFAULT_BUY":
                await self._handle_setting_default_buy(query)
            elif data == "SETTING_COOLDOWN":
                await self._handle_setting_cooldown(query)
            elif data == "SETTING_MANAGE_ALERTS":
                await self._handle_setting_manage_alerts(query)
            
            else:
                await query.edit_message_text("❌ Unknown command.")
                
        except Exception as e:
            logger.error(f"Callback query error: {e}")
            try:
                await query.edit_message_text("❌ Error processing request.")
            except:
                pass
    
    async def _handle_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """معالج الأخطاء العام"""
        try:
            self.performance_stats['errors_count'] += 1
            
            error_msg = f"Error occurred: {context.error}"
            logger.error(error_msg)
            
            if update and hasattr(update, 'effective_user'):
                user_id = update.effective_user.id
                if user_id == self.config.ADMIN_ID:
                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text="❌ An error occurred. Please try again or use /menu."
                        )
                    except:
                        pass
        except Exception as e:
            logger.error(f"Error handler failed: {e}")
    
    # ——————————— معالجات القوائم والعرض ———————————
    async def _show_main_menu(self, query):
        """عرض القائمة الرئيسية"""
        try:
            # جلب معلومات أساسية
            sol_balance = await self.solana_service.get_sol_balance(self.config.PUBLIC_KEY)
            sol_price = await self.price_service.get_sol_price_usd()
            usd_balance = sol_balance * sol_price
            
            active_positions = self.db_manager.get_active_positions()
            stats = self.db_manager.get_trading_stats()
            
            menu_message = f"""
📱 <b>Trading Dashboard</b>

💳 <b>Wallet</b>
━━━━━━━━━━━━━━━━
💰 SOL: {EnhancedFormatUtils.format_price(sol_balance)} 
💵 USD: ${EnhancedFormatUtils.format_price(usd_balance)}

📊 <b>Portfolio</b>
━━━━━━━━━━━━━━━━
🔥 Active Positions: {len(active_positions)}
📈 Total P&L: {EnhancedFormatUtils.format_pnl(stats.total_pnl)}
🎯 Win Rate: {stats.win_rate:.1f}%

Choose an option below:
            """
            
            keyboard = EnhancedUIBuilder.build_main_menu()
            
            await query.edit_message_text(
                menu_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
        except Exception as e:
            logger.error(f"Show main menu error: {e}")
            await query.edit_message_text("❌ Error loading menu.")
    
    async def _show_trade_menu(self, query):
        """عرض قائمة التداول"""
        try:
            user_id = query.from_user.id
            
            trade_message = """
🔥 <b>Trading Interface</b>

<b>Quick Actions</b>
━━━━━━━━━━━━━━━━
Choose your trading action below:

• 🟢 <b>Buy Token</b> - Purchase new tokens
• 🔴 <b>Sell Position</b> - Close existing positions
• 📊 <b>Analyze Token</b> - Research before trading
• ⚙️ <b>Settings</b> - Configure trading parameters

💡 <b>Tip:</b> You can also send a token address directly for quick analysis!
            """
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🟢 Buy Token", callback_data="TRADE_BUY"),
                    InlineKeyboardButton("🔴 Sell Position", callback_data="TRADE_SELL")
                ],
                [
                    InlineKeyboardButton("📊 Analyze Token", callback_data="TRADE_ANALYZE"),
                    InlineKeyboardButton("⚙️ Settings", callback_data="MENU_SETTINGS")
                ],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="BACK_MAIN")]
            ])
            
            await query.edit_message_text(
                trade_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
            # تحديث حالة المستخدم
            self.user_states[user_id] = {'state': 'TRADE_MENU'}
            
        except Exception as e:
            logger.error(f"Show trade menu error: {e}")
            await query.edit_message_text("❌ Error loading trade menu.")
    
    async def _show_positions_menu(self, query):
        """عرض قائمة الصفقات"""
        try:
            active_positions = self.db_manager.get_active_positions()
            
            if not active_positions:
                message = """
📊 <b>Active Positions</b>

🔍 No active positions found.
Ready to start trading?
                """
                
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🟢 Start Trading", callback_data="MENU_TRADE")],
                    [InlineKeyboardButton("🔙 Back to Menu", callback_data="BACK_MAIN")]
                ])
                
                await query.edit_message_text(
                    message,
                    reply_markup=keyboard,
                    parse_mode='HTML'
                )
                return
            
            # عرض الصفقات النشطة
            positions_text = "📊 <b>Active Positions</b>\n\n"
            total_pnl = 0
            
            for i, position in enumerate(active_positions[:5], 1):
                try:
                    
                    await self.price_service.get_enhanced_token_info(
                        position.token_address
                    )
                    
                    current_value = position.tokens_amount * token_info.price_usd
                    initial_value = position.buy_amount_sol * position.buy_price_usd
                    pnl_usd = current_value - initial_value
                    pnl_percentage = (pnl_usd / initial_value) * 100 if initial_value > 0 else 0
                    
                    total_pnl += pnl_usd
                    
                    duration = EnhancedFormatUtils.format_duration(position.created_at)
                    pnl_str = EnhancedFormatUtils.format_pnl(pnl_usd)
                    percentage_str = EnhancedFormatUtils.format_percentage(pnl_percentage)
                    
                    positions_text += f"""
<b>{i}. {position.token_symbol}</b>
💰 Entry: ${EnhancedFormatUtils.format_price(position.buy_price_usd)}
📊 Current: ${EnhancedFormatUtils.format_price(token_info.price_usd)}
{pnl_str} ({percentage_str})
⏰ {duration}
━━━━━━━━━━━━━━━━
"""
                except Exception as e:
                    logger.error(f"Error processing position {position.token_symbol}: {e}")
                    continue
            
            positions_text += f"\n📈 <b>Total P&L: {EnhancedFormatUtils.format_pnl(total_pnl)}</b>"
            
            # إنشاء أزرار للصفقات
            keyboard_buttons = []
            for position in active_positions[:5]:
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        f"📊 {position.token_symbol}",
                        callback_data=f"POSITION_DETAILS_{position.token_address}"
                    )
                ])
            
            keyboard_buttons.extend([
                [InlineKeyboardButton("🔄 Refresh", callback_data="POSITIONS_REFRESH")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="BACK_MAIN")]
            ])
            
            keyboard = InlineKeyboardMarkup(keyboard_buttons)
            
            await query.edit_message_text(
                positions_text,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
        except Exception as e:
            logger.error(f"Show positions menu error: {e}")
            await query.edit_message_text("❌ Error loading positions.")
    
    async def _show_wallet_info(self, query):
        """عرض معلومات المحفظة"""
        try:
            sol_balance = await self.solana_service.get_sol_balance(self.config.PUBLIC_KEY)
            sol_price = await self.price_service.get_sol_price_usd()
            usd_balance = sol_balance * sol_price
            
            active_positions = self.db_manager.get_active_positions()
            total_portfolio_value = usd_balance
            
            for position in active_positions:
                try:
                    token_info = await self.price_service.get_enhanced_token_info(
                        position.token_address
                    )
                    position_value = position.tokens_amount * token_info.price_usd
                    total_portfolio_value += position_value
                except Exception:
                    continue
            
            wallet_message = f"""
💳 <b>Wallet Overview</b>

💰 <b>SOL Balance</b>
━━━━━━━━━━━━━━━━
🪙 Amount: {EnhancedFormatUtils.format_price(sol_balance)} SOL
💵 USD Value: ${EnhancedFormatUtils.format_price(usd_balance)}
📊 SOL Price: ${EnhancedFormatUtils.format_price(sol_price)}

📊 <b>Portfolio Summary</b>
━━━━━━━━━━━━━━━━
💎 Total Value: ${EnhancedFormatUtils.format_price(total_portfolio_value)}
🔥 Active Positions: {len(active_positions)}
💰 Available SOL: {EnhancedFormatUtils.format_price(sol_balance)}

🏦 <b>Account Details</b>
━━━━━━━━━━━━━━━━
🔑 Address: <code>{self.config.PUBLIC_KEY[:12]}...{self.config.PUBLIC_KEY[-12:]}</code>
⏰ Last Update: {datetime.now().strftime('%H:%M:%S')}
            """
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="WALLET_REFRESH")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="BACK_MAIN")]
            ])
            
            await query.edit_message_text(
                wallet_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
        except Exception as e:
            logger.error(f"Show wallet info error: {e}")
            await query.edit_message_text("❌ Error loading wallet info.")
    
    async def _show_analytics_menu(self, query):
        """عرض قائمة التحليلات"""
        try:
            analytics_message = """
📈 <b>Trading Analytics</b>

Choose an analytics option below:
            """
            
            keyboard = EnhancedUIBuilder.build_analytics_menu()
            
            await query.edit_message_text(
                analytics_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
        except Exception as e:
            logger.error(f"Show analytics menu error: {e}")
            await query.edit_message_text("❌ Error loading analytics menu.")
    
    async def _show_settings_menu(self, query):
        """عرض قائمة الإعدادات"""
        try:
            settings_message = """
⚙️ <b>Bot Settings</b>

Choose a setting to configure:
            """
            
            keyboard = EnhancedUIBuilder.build_settings_menu()
            
            await query.edit_message_text(
                settings_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
        except Exception as e:
            logger.error(f"Show settings menu error: {e}")
            await query.edit_message_text("❌ Error loading settings menu.")
    
    async def _show_stats_menu(self, query):
        """عرض إحصائيات التداول"""
        try:
            stats = self.db_manager.get_trading_stats()
            active_positions = self.db_manager.get_active_positions()
            
            # حساب P&L الحالي للصفقات النشطة
            current_unrealized_pnl = 0
            for position in active_positions:
                try:
                    token_info = await self.price_service.get_enhanced_token_info(
                        position.token_address
                    )
                    current_value = position.tokens_amount * token_info.price_usd
                    initial_value = position.buy_amount_sol * position.buy_price_usd
                    current_unrealized_pnl += (current_value - initial_value)
                except Exception:
                    continue
            
            # حساب معدلات الأداء
            uptime = datetime.now() - self.performance_stats['start_time']
            uptime_str = EnhancedFormatUtils.format_duration(uptime)
            
            stats_message = f"""
📈 <b>Trading Statistics</b>

💰 <b>Performance Overview</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 Total P&L: {EnhancedFormatUtils.format_pnl(stats.total_pnl)}
💎 Unrealized P&L: {EnhancedFormatUtils.format_pnl(current_unrealized_pnl)}
🎯 Win Rate: {stats.win_rate:.1f}%
🔥 Active Positions: {len(active_positions)}

📊 <b>Trade Statistics</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📈 Total Trades: {stats.total_trades}
🟢 Winning Trades: {stats.winning_trades}
🔴 Losing Trades: {stats.losing_trades}
💰 Best Trade: {EnhancedFormatUtils.format_pnl(stats.best_trade)}
📉 Worst Trade: {EnhancedFormatUtils.format_pnl(stats.worst_trade)}

⚡ <b>System Performance</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏰ Uptime: {uptime_str}
🔧 Commands: {self.performance_stats['commands_processed']}
💼 Trades Executed: {self.performance_stats['trades_executed']}
⚠️ Errors: {self.performance_stats['errors_count']}
            """
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📊 Details", callback_data="ANALYTICS_STATS"),
                    InlineKeyboardButton("🔄 Refresh", callback_data="STATS_REFRESH")
                ],
                [InlineKeyboardButton("🔙 Back to Analytics", callback_data="MENU_ANALYTICS")]
            ])
            
            await query.edit_message_text(
                stats_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
        except Exception as e:
            logger.error(f"Show stats menu error: {e}")
            await query.edit_message_text("❌ Error loading statistics.")
    
    async def _show_performance_menu(self, query):
        """عرض أداء التداول"""
        try:
            performance_message = """
📊 <b>Trading Performance</b>

Detailed performance charts and metrics:
            """
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📈 Daily Performance", callback_data="PERF_DAILY")],
                [InlineKeyboardButton("📅 Monthly Performance", callback_data="PERF_MONTHLY")],
                [InlineKeyboardButton("🔙 Back to Analytics", callback_data="MENU_ANALYTICS")]
            ])
            
            await query.edit_message_text(
                performance_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
        except Exception as e:
            logger.error(f"Show performance menu error: {e}")
            await query.edit_message_text("❌ Error loading performance data.")
    
    # ——————————— معالجات التداول ———————————
    async def _handle_token_address_input(self, update, token_address):
        """معالج إدخال عنوان التوكن"""
        try:
            if not self._is_valid_solana_address(token_address):
                await update.message.reply_text("❌ Invalid token address format.")
                return
            
            # جلب معلومات التوكن
            loading_msg = await update.message.reply_text("🔍 Analyzing token...")
            
            token_info = await self.price_service.get_enhanced_token_info(token_address)
            
            if token_info.price_usd <= 0:
                await loading_msg.edit_text("❌ Token not found or has no price data.")
                return
            
            # عرض معلومات التوكن
            analysis_message = f"""
🔍 <b>Token Analysis</b>

<b>{token_info.token_symbol}</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💰 Price: ${EnhancedFormatUtils.format_price(token_info.price_usd)}
📊 Market Cap: ${EnhancedFormatUtils.human_format_number(token_info.market_cap_usd)}
💧 Liquidity: ${EnhancedFormatUtils.human_format_number(token_info.liquidity_usd)}
📈 Volume (24h): ${EnhancedFormatUtils.human_format_number(token_info.volume_24h)}

🎯 <b>Price Changes</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🕐 24h: {EnhancedFormatUtils.format_percentage(token_info.price_change_24h)}

📋 <b>Token Address</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<code>{token_address}</code>

Ready to trade?
            """
            
            keyboard = EnhancedUIBuilder.build_token_analysis_actions(token_address)
            
            await loading_msg.edit_text(
                analysis_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
        except Exception as e:
            logger.error(f"Token analysis error: {e}")
            await update.message.reply_text("❌ Error analyzing token.")
    
    async def _execute_buy_trade(self, update, token_address: str, amount_sol: float):
        """تنفيذ صفقة شراء"""
        try:
            # التحقق من الرصيد
            sol_balance = await self.solana_service.get_sol_balance(self.config.PUBLIC_KEY)
            
            if sol_balance < amount_sol + 0.01:  # +0.01 للرسوم
                await update.message.reply_text(
                    f"❌ Insufficient SOL balance.\n"
                    f"Available: {sol_balance:.4f} SOL\n"
                    f"Required: {amount_sol + 0.01:.4f} SOL"
                )
                return
            
            # جلب معلومات التوكن
            token_info = await self.price_service.get_enhanced_token_info(token_address)
            
            if token_info.price_usd <= 0:
                await update.message.reply_text("❌ Token price data not available.")
                return
            
            loading_msg = await update.message.reply_text("⚡ Executing buy order...")
            
            # محاكاة تنفيذ الصفقة (في التطبيق الحقيقي يتم استخدام Solana SDK)
            await asyncio.sleep(2)  # محاكاة وقت التنفيذ
            
            # حساب كمية التوكنات
            tokens_amount = (amount_sol * token_info.price_sol) / token_info.price_usd
            
            # إنشاء Position جديدة
            position = Position(
                token_address=token_address,
                token_name=token_info.token_name,
                token_symbol=token_info.token_symbol,
                buy_amount_sol=amount_sol,
                buy_price_usd=token_info.price_usd,
                tokens_amount=tokens_amount,
                created_at=datetime.now()
            )
            
            # حفظ في قاعدة البيانات
            self.db_manager.save_position(position)
            
            # رسالة النجاح
            success_message = f"""
✅ <b>Buy Order Executed</b>

🎯 <b>Trade Details</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🪙 Token: <b>{token_info.token_symbol}</b>
💰 Amount: {amount_sol} SOL
📊 Price: ${EnhancedFormatUtils.format_price(token_info.price_usd)}
🔢 Tokens: {EnhancedFormatUtils.human_format_number(tokens_amount)}
💵 Value: ${EnhancedFormatUtils.format_price(amount_sol * token_info.price_sol)}

⏰ <b>Executed:</b> {datetime.now().strftime('%H:%M:%S')}
🔗 <b>Address:</b> <code>{token_address[:12]}...{token_address[-12:]}</code>

🎉 Position added to your portfolio!
            """
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📊 View Position", callback_data=f"POSITION_DETAILS_{token_address}"),
                    InlineKeyboardButton("🔴 Quick Sell", callback_data=f"SELL_QUICK_{token_address}")
                ],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="BACK_MAIN")]
            ])
            
            await loading_msg.edit_text(
                success_message,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            
            self.performance_stats['trades_executed'] += 1
            
        except Exception as e:
            logger.error(f"Execute buy trade error: {e}")
            await update.message.reply_text("❌ Error executing buy order.")
    
    async def _execute_sell_trade(self, update, token_address: str, percentage: float):
        """تنفيذ صفقة بيع"""
        try:
            # البحث عن الصفقة
            active_positions = self.db_manager.get_active_positions()
            position = next((p for p in active_positions if p.token_address == token_address), None)
            
            if not position:
                await update.message.reply_text("❌ No active position found for this token.")
                return
            
            # جلب معلومات التوكن الحالية
            token_info = await self.price_service.get_enhanced_token_info(token_address)
            
            loading_msg = await update.message.reply_text("⚡ Executing sell order...")
            
            # حساب كمية البيع
            tokens_to_sell = position.tokens_amount * (percentage / 100)
            current_value = tokens_to_sell * token_info.price_usd
            initial_value = (position.buy_amount_sol * position.buy_price_usd) * (percentage / 100)
            pnl = current_value - initial_value
            pnl_percentage = (pnl / initial_value) * 100 if initial_value > 0 else 0
            
            # محاكاة تنفيذ البيع
            await asyncio.sleep(2)
            
            # تحديث الصفقة
            if percentage >= 100:
                # بيع كامل - حذف الصفقة
                self.db_manager.close_position(token_address, pnl)
            else:
                # بيع جزئي - تحديث الصفقة
                remaining_tokens = position.tokens_amount - tokens_to_sell
                remaining_amount = position.buy_amount_sol * (remaining_tokens / position.tokens_amount)
                
                position.tokens_amount = remaining_tokens
                position.buy_amount_sol = remaining_amount
                self.db_manager.update_position(position.token_address, tokens_amount=remaining_tokens, buy_amount_sol=remaining_amount)
            
            # رسالة النجاح
            success_message = f"""
m