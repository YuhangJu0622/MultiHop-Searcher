import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from agent.agent_loop import react_agent

async def main():
    # 您可以在这里修改为您想要测试的问题
    question = "Who is the author of the article that introduces the methodology of prosopography and demographic analysis of colonial social structures, analyzes the structural evolution of encomienda and hacienda systems, critiques historiographical gaps in prior political-centric approaches to colonial Spanish America, and was first published in the 1972 journal issue that analyzes the economic impacts of import substitution industrialization policies in Latin America and critiques political-centric historiographical approaches to colonial development?"
    
    print(f"开始测试问题: {question}")
    print("-" * 50)
    
    try:
        # 调用 react_agent
        answer = await react_agent(question)
        
        print("-" * 50)
        print("测试完成！")
        print(f"最终答案: {answer}")
        
    except Exception as e:
        print(f"测试过程中发生错误: {e}")

if __name__ == "__main__":
    asyncio.run(main())
