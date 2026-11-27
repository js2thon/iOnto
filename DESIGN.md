# iOnto Frontend Interface Design Spec

> 基于 Zachary Zhang《Frontend Rules That Get AI to Build Clean Interfaces》的 5 个模块，针对 iOnto（Streamlit 企业本体建模工作台）做适配。
> 目标：让第一版界面干净、层级清晰、少干扰。不要期望一次生成就完美，先保证不丑，再迭代细节。

## 1. Layout and information
- 禁止盒子套盒子。Streamlit 中避免多层 `st.container` / `st.expander` / 嵌套 `columns` 叠加边框。
- 禁止 tagline、副标题、二次说明、解释性 caption。能通过对齐和间距表达的，不要写文字。
- 用对齐和留白区分信息层级，而不是用边框、分割线或解释文字。

## 2. Visuals, color and whitespace
- 整体参考 Apple 官网极简风格。
- 颜色比例：90% 中性黑白灰，10% 单一强调色（仅用于主操作按钮或关键状态高亮）。
- 禁止渐变、高饱和色、霓虹色、复杂阴影卡片。
- 标题更粗更深，正文更浅更淡。
- 留白增加到常规 Streamlit 页面的约 1.5 倍。宁可留空，也不要填满。

### 推荐色值
- 页面背景：`#fafafa`
- 卡片 / 侧边栏背景：`#ffffff`
- 主文字：`#181818`
- 次文字 / caption：`#6A6A6A`
- 边框：`#e8e8e8` / `#e0e0e0`
- 主按钮（唯一强调色）：背景 `#181818`，文字 `#ffffff`
- 次要按钮：白底 + 浅边框

## 3. Type, grid and icons
- 字体：使用系统无衬线（Streamlit 默认即可，避免强行注入外部字体增加复杂度）。
- 字号与字重：
  - 标题：18px，字重 600–700
  - 正文：14–16px，字重 400–500
- 文字颜色：主文字 `#181818`，次文字 `#6A6A6A`
- 间距网格：所有 padding / margin / gap 必须是 4px 的整数倍（8 / 12 / 16 / 24 / 32），不允许随意数值。
- 圆角：小按钮 8px，卡片 / 容器 / Expander 12px。
- 图标：优先使用简洁 emoji 或 Streamlit 内置图标，视觉大小约 18–20px，保持统一。

## 4. Interaction
- 次要操作默认隐藏或折叠：不要一排全亮 Edit / Delete / 复制等按钮。优先放在 expander、菜单或 hover/focus 后显示。
- 主操作按钮保持高可见度（黑底白字），次要按钮使用白底描边。
- 按钮文案必须明确动作，例如：
  - 「保存草稿」
  - 「执行预览」
  - 「正式执行」
  - 「发布版本」
  避免模糊或过长的说明性文案。
- 表单提交后及时给出简洁反馈，避免大段解释。

## 5. Render acceptance
- 以浏览器真实渲染结果为准，而不是代码里的计算值。
- 修改 CSS 或布局后，必须本地运行 `streamlit run ontology_studio_v3.py`，检查：
  - 对齐是否整齐
  - 文字是否换行溢出
  - 留白是否充足
  - 是否出现多余边框或重叠
- 只有真实页面通过验收后，才算完成。

## Streamlit 落地注意事项
- CSS 统一写在 `ontology_studio_v3.py` 顶部的 `st.markdown(..., unsafe_allow_html=True)` 中。
- 尽量减少 `st.caption`、`st.info`、`st.warning` 的默认展示，非必要信息放进 expander 或只在出错时出现。
- Metric、表单、Tab 保持扁平结构，避免层层嵌套容器。
- 映射工作台、问答页、版本管理等页面统一遵循本规范，不要各自发明样式。
