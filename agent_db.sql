/*
 Navicat Premium Dump SQL

 Source Server         : MySQL
 Source Server Type    : MySQL
 Source Server Version : 80046 (8.0.46)
 Source Host           : localhost:3306
 Source Schema         : agent_db

 Target Server Type    : MySQL
 Target Server Version : 80046 (8.0.46)
 File Encoding         : 65001

 Date: 27/08/2026 15:17:09
*/

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ----------------------------
-- Table structure for products (先建，inventory 有外键引用)
-- ----------------------------
DROP TABLE IF EXISTS `inventory`;
DROP TABLE IF EXISTS `conversations`;
DROP TABLE IF EXISTS `orders`;
DROP TABLE IF EXISTS `products`;

CREATE TABLE `products`  (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL COMMENT '产品名称',
  `spec` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT '' COMMENT '规格',
  `target_fish` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT '' COMMENT '适用鱼种',
  `retail_price` decimal(10, 2) NULL DEFAULT 0.00 COMMENT '零售价',
  `wholesale_price` decimal(10, 2) NULL DEFAULT 0.00 COMMENT '批发价',
  `description` text CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL COMMENT '产品描述',
  PRIMARY KEY (`id`) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 8 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for inventory
-- ----------------------------
CREATE TABLE `inventory`  (
  `id` int NOT NULL AUTO_INCREMENT,
  `product_id` int NOT NULL COMMENT '对应products.id',
  `stock` int NULL DEFAULT 0 COMMENT '当前库存',
  `alert_line` int NULL DEFAULT 50 COMMENT '预警线',
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `product_id`(`product_id` ASC) USING BTREE,
  CONSTRAINT `inventory_ibfk_1` FOREIGN KEY (`product_id`) REFERENCES `products` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE = InnoDB AUTO_INCREMENT = 8 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for orders
-- ----------------------------
CREATE TABLE `orders`  (
  `id` int NOT NULL AUTO_INCREMENT,
  `order_no` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL COMMENT '订单号',
  `customer_name` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT '' COMMENT '客户名',
  `phone` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT NULL COMMENT '联系电话',
  `product_id` int NULL DEFAULT NULL COMMENT '关联产品ID',
  `product_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT '' COMMENT '产品名(冗余)',
  `quantity` int NULL DEFAULT 0 COMMENT '数量',
  `total_price` decimal(10, 2) NULL DEFAULT 0.00 COMMENT '总价',
  `status` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT '未发货' COMMENT '状态(未发货/已发货)',
  `ship_company` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT '' COMMENT '物流公司',
  `tracking_no` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT '' COMMENT '运单号',
  `shipped_at` datetime NULL DEFAULT NULL COMMENT '发货时间',
  `created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP COMMENT '下单时间',
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `product_id`(`product_id` ASC) USING BTREE,
  CONSTRAINT `orders_ibfk_1` FOREIGN KEY (`product_id`) REFERENCES `products` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE = InnoDB AUTO_INCREMENT = 6 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for conversations
-- ----------------------------
CREATE TABLE `conversations`  (
  `id` int NOT NULL AUTO_INCREMENT,
  `session_id` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT '' COMMENT '会话ID',
  `role` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT '' COMMENT 'user 或 assistant',
  `content` text CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL COMMENT '消息内容',
  `created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP COMMENT '时间',
  PRIMARY KEY (`id`) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 1 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci ROW_FORMAT = Dynamic;

SET FOREIGN_KEY_CHECKS = 1;

-- ============================================
-- 升级脚本（已有库的增量迁移）：
-- 若已存在 `orders` 表，可执行以下 ALTER 升级结构
-- ============================================
ALTER TABLE `orders`
    ADD COLUMN IF NOT EXISTS `phone` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT NULL COMMENT '联系电话' AFTER `customer_name`,
    ADD COLUMN IF NOT EXISTS `product_id` int NULL DEFAULT NULL COMMENT '关联产品ID' AFTER `product_name`,
    ADD INDEX IF NOT EXISTS `product_id`(`product_id` ASC) USING BTREE,
    ADD CONSTRAINT `orders_product_fk` FOREIGN KEY (`product_id`) REFERENCES `products` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `orders` MODIFY COLUMN `status` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT '未发货' COMMENT '状态(未发货/已发货)';

-- ----------------------------
-- 初始数据
-- ----------------------------
INSERT INTO `products` (`id`, `name`, `spec`, `target_fish`, `retail_price`, `wholesale_price`, `description`) VALUES
(1, '红虫颗粒', '200g/包，1箱=10包', '鲫鱼、鲤鱼', 6.00, 4.00, '高蛋白浓腥味，野钓黑坑通用'),
(2, '九一八', '150g/包，1箱=10包', '鲫鱼、鲤鱼', 5.00, 3.50, '经典广谱饵，四季可用'),
(3, '螺鲤3号', '200g/包，1箱=10包', '鲤鱼', 7.00, 5.00, '薯香味浓，专攻大鲤鱼'),
(4, '蓝鲫X5', '100g/包，1箱=10包', '鲫鱼、鲤鱼', 8.00, 6.00, '腥香结合，诱鱼快'),
(5, '速攻2号', '200g/包，1箱=10包', '鲫鱼', 6.00, 4.50, '奶香浓郁，秋冬鲫鱼必备');

INSERT INTO `inventory` (`id`, `product_id`, `stock`, `alert_line`) VALUES
(1, 1, 320, 50),
(2, 2, 80, 50),
(3, 3, 30, 50),
(4, 4, 150, 50),
(5, 5, 0, 50);

INSERT INTO `orders` (`id`, `order_no`, `customer_name`, `product_name`, `quantity`, `total_price`, `status`, `created_at`) VALUES
(1, 'DD20240801', '张老板', '红虫颗粒+螺鲤3号', 150, 850.00, '已发货', '2024-08-01 10:30:00'),
(2, 'DD20240815', '李老板', '九一八', 200, 700.00, '待发货', '2024-08-15 14:20:00'),
(3, 'DD20240820', '王老板', '蓝鲫X5', 500, 3000.00, '已完成', '2024-08-20 09:15:00'),
(4, 'DD20240901', '赵老板', '速攻2号', 100, 450.00, '已发货', '2024-09-01 16:45:00'),
(5, 'DD20240910', '孙老板', '红虫颗粒', 300, 1200.00, '待发货', '2024-09-10 11:00:00');
