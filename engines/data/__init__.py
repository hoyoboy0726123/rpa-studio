# -*- coding: utf-8 -*-
"""data engine — 資料 / Excel 動作組(無 GUI、無瀏覽器,純檔案 I/O)。

import 本 package 即觸發 data.* / excel.* action 註冊。
對齊「批次抓資料填 Excel、拆檔、跨檔比對」三大能力:
  - robust_loader : 容錯讀表(多編碼 / 跳空列 / 欄名與數值與日期正規化)
  - excel_ops     : 純函式層(寫入、附加、依欄拆檔、規則拆檔、跨檔比對)
  - actions       : 用 @action 把上述能力包成 runner 可分派的步驟
"""
from . import actions  # noqa: F401  匯入即註冊 data.* / excel.*
