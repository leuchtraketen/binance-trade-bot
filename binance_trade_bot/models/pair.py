from sqlalchemy import Column, Float, ForeignKey, Integer, String, func, or_, select
from sqlalchemy.orm import column_property, relationship

from .base import Base
from .coin import Coin


class Pair(Base):
    __tablename__ = "pairs"

    id = Column(Integer, primary_key=True)

    from_coin_id = Column(String, ForeignKey("coins.symbol"))
    from_coin = relationship("Coin", foreign_keys=[from_coin_id], lazy="joined")

    to_coin_id = Column(String, ForeignKey("coins.symbol"))
    to_coin = relationship("Coin", foreign_keys=[to_coin_id], lazy="joined")

    ratio = Column(Float)

    from_coin_price = Column(Float)

    to_coin_price = Column(Float)

    enabled = column_property(
        select([func.count(Coin.symbol) == 2])
        .where(or_(Coin.symbol == from_coin_id, Coin.symbol == to_coin_id))
        .where(Coin.enabled.is_(True))
        .scalar_subquery()
    )

    def __init__(self, from_coin: Coin, to_coin: Coin, ratio=None, from_coin_price=None, to_coin_price=None):
        self.from_coin = from_coin
        self.to_coin = to_coin
        self.ratio = ratio
        self.from_coin_price = from_coin_price
        self.to_coin_price = to_coin_price

    def __repr__(self):
        return f"<{self.from_coin_id}->{self.to_coin_id} :: {self.ratio} = {self.from_coin_price} / {self.to_coin_price}>"

    def info(self):
        return {
            "from_coin": self.from_coin.info(),
            "to_coin": self.to_coin.info(),
            "ratio": self.ratio,
            "from_coin_price": self.from_coin_price,
            "to_coin_price": self.to_coin_price,
        }
