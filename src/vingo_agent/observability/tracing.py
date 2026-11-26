"""Trace (V2.2 mục 9): chat.turn → context → model.request → tool.call → backend.http → response.check;
action.confirm → backend.http → lookup. Không ghi nội dung tin nhắn hay tham số tool vào span."""
