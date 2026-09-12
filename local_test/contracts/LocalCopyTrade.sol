// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

contract LocalUSDG {
    string public constant name = "Local USDG";
    string public constant symbol = "USDG";
    uint8 public constant decimals = 18;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(uint256 supply) {
        totalSupply = supply;
        balanceOf[msg.sender] = supply;
        emit Transfer(address(0), msg.sender, supply);
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 approved = allowance[from][msg.sender];
        require(approved >= amount, "allowance");
        if (approved != type(uint256).max) allowance[from][msg.sender] = approved - amount;
        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) internal {
        require(to != address(0) && balanceOf[from] >= amount, "transfer");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
    }
}

contract LocalFixedRatePool {
    LocalUSDG public immutable usdg;
    uint256 public constant USDG_PER_ETH = 1000;

    event Swap(
        address indexed trader,
        address indexed tokenIn,
        address indexed tokenOut,
        uint256 amountIn,
        uint256 amountOut
    );

    constructor(address token) payable { usdg = LocalUSDG(token); }
    receive() external payable {}

    function buy() external payable returns (uint256 amountOut) {
        require(msg.value > 0, "zero input");
        amountOut = msg.value * USDG_PER_ETH;
        require(usdg.transfer(msg.sender, amountOut), "token out");
        emit Swap(msg.sender, address(0), address(usdg), msg.value, amountOut);
    }

    function sell(uint256 amountIn) external returns (uint256 amountOut) {
        require(amountIn > 0, "zero input");
        amountOut = amountIn / USDG_PER_ETH;
        require(address(this).balance >= amountOut, "eth reserve");
        require(usdg.transferFrom(msg.sender, address(this), amountIn), "token in");
        (bool sent,) = msg.sender.call{value: amountOut}("");
        require(sent, "eth out");
        emit Swap(msg.sender, address(usdg), address(0), amountIn, amountOut);
    }
}
