# 今天吃什么 · eat-what-easy

一个简单的 Astrbot 关键词回复随机“今天吃什么”菜品图片的插件。

包含“吃什么”、“喝什么”关键词，会分别回复随机菜品、饮品图片。

基于 https://www.npmjs.com/package/koishi-plugin-eat-what-pro/v/1.0.0 项目做的修改，增加了时间关键词匹配，比如发送“中午吃什么”，会回复“中午吃XX”。可以在插件配置中自行修改。

菜品图片包含在 `\assets\foods` 中，可自行添加，发送时会附上图片文件名作为菜品名称。

还有“额外”菜品包含在 `\assets\extra_foods` 中，内含一些“不是食物的图片”作为彩蛋菜单，会有较低几率匹配，可以自行添加。

饮品图片包含在 `\assets\drinks` `\assets\extra_drinks` 中，可自行添加。

## 回复示例

![回复示例](./docs/example.png)
