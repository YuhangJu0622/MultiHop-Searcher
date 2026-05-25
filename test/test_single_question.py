import asyncio
import sys
import os
from dotenv import load_dotenv

# 将项目根目录添加到 sys.path 中
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

# 加载 .env 文件中的环境变量
load_dotenv()

from agent_loop import react_agent

async def main():
    # 您可以在这里修改为您想要测试的问题
    question = "一位欧洲学者的某项开源硬件项目，其灵感源于一个著名的元胞自动机，该项目的一个早期物理设计从四边形框架演变为更稳固的三角形结构。这位在机械工程某一分支领域深耕的学者，从大学教职岗位上引退后，继续领导一个与该项目相关的商业实体。该实体在21世纪10年代中期停止了在其欧洲本土的主要交易，但其在一个亚洲国家的业务得以延续。这个商业实体的英文名称是什么？要求格式形如：Alibaba Group Limited。"
    
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
