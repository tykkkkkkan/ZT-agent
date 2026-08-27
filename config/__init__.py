"""
config/__init__.py

Day4: 使用 pymysql 作为 MySQL 驱动（Windows 下 mysqlclient 常编译失败，
pymysql 纯 Python 实现，无需编译，兼容 MySQLdb 接口）。
"""
import pymysql

pymysql.install_as_MySQLdb()
